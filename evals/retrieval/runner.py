"""US-033: retrieval eval runner.

Sweeps the 50-question golden set in `retrieval_gold.yaml` across the three
retrieval modes — vector, keyword, hybrid — using the real
`backend/retrieval.py` functions (so a future PR that breaks retrieval
breaks the eval). Computes recall@{1,3,5,10} (per-chunk partial credit),
MRR, and nDCG@5 (binary relevance, log2 position discount) per question,
then aggregates means per mode and per (mode × category).

Outputs:
    evals/retrieval/results/<ISO-timestamp>.json   — full per-question detail
    evals/retrieval/summary.md                     — two-table markdown
                                                     fragment bracketed by
                                                     EVAL_SUMMARY markers
                                                     for `docs/evals.md`
                                                     (US-034) to embed

Run:
    python -m evals.retrieval.runner
    python -m evals.retrieval.runner --mode vector       # single mode
    python -m evals.retrieval.runner --out /tmp/pr.json  # CI use

Reads env:
    SUPABASE_URL                       — local: http://127.0.0.1:54321
    SUPABASE_SERVICE_ROLE_KEY          — eval bypasses RLS; corpus chunks
                                         live under a sentinel user
    OPENAI_API_KEY                     — required for vector + hybrid
    CORPUS_SEED_DATABASE_URL | DATABASE_URL  — for the chunk_id→stable_id map

Determinism caveat: OpenAI embeddings are not strictly bit-deterministic
across calls. In practice the values agree to floating-point precision for
fixed input + fixed model version, so recall/MRR/nDCG numbers are stable
modulo embedding-API drift. Hard-blocking on byte-identical JSON across
runs would create flake; CI uses comment-only delta tables (US-035).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import yaml
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from retrieval import (  # noqa: E402
    SearchDocumentsResult,
    hybrid_search,
    keyword_only_search,
    search_documents,
)

log = logging.getLogger("agentic_rag.evals.retrieval")

DEFAULT_QUESTIONS = Path(__file__).resolve().parent / "retrieval_gold.yaml"
DEFAULT_GENERATION_GOLD = Path(__file__).resolve().parent / "generation_gold.yaml"
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_SUMMARY = Path(__file__).resolve().parent / "summary.md"

MODES = ("vector", "keyword", "hybrid")
TOP_K = 10  # Retrieve 10; metrics at k ∈ {1,3,5,10} are computed from this list.
RECALL_KS = (1, 3, 5, 10)
CATEGORY_ORDER = ("single_chunk", "multi_hop", "adversarial", "paraphrase")

# US-036: generation + judge.
# Generator and judge MUST be different model families to avoid same-model
# scoring bias (a well-known evaluation pitfall). Generator is small + fast
# + cheap; judge is the more capable model since it does the harder job
# (reading the question, reference, context, and answer to score two
# dimensions).
GENERATION_MODEL = "gpt-4o-mini"
JUDGE_MODEL = "claude-haiku-4-5"
GENERATION_SEED = 42
GENERATION_MAX_TOKENS = 400
JUDGE_MAX_TOKENS = 200
TOP_K_FOR_GENERATION = 5  # Context fed to the generator is the mode's top-5.

GENERATION_PROMPT_SYSTEM = (
    "You are a customer service agent for Acme Co. Answer the user's "
    "question using ONLY information present in the provided context. If "
    "the context doesn't contain the answer, say so explicitly. Keep "
    "answers concise (2-3 sentences) and factual."
)

GENERATION_PROMPT_USER_TEMPLATE = (
    "Context (from retrieved support documents):\n"
    "---\n"
    "{context}\n"
    "---\n\n"
    "Question: {question}\n\n"
    "Answer:"
)

JUDGE_PROMPT_TEMPLATE = (
    "You are evaluating an AI assistant's answer to a customer service "
    "question.\n\n"
    "Question: {question}\n\n"
    "Reference answer (the gold standard, hand-authored):\n"
    "{reference}\n\n"
    "Retrieved context (what the AI saw when generating its answer):\n"
    "---\n"
    "{context}\n"
    "---\n\n"
    "AI's generated answer:\n"
    "{answer}\n\n"
    "Score two dimensions on a 1-5 integer scale:\n\n"
    "1. **Faithfulness**: Does the AI's answer use ONLY information "
    "present in the retrieved context, without hallucinating?\n"
    "   - 5 = every claim grounded in the context\n"
    "   - 4 = mostly grounded, minor unsupported phrasing\n"
    "   - 3 = significant claims unsupported by context\n"
    "   - 2 = mostly unsupported by context\n"
    "   - 1 = fabrications throughout\n\n"
    "2. **Helpfulness**: Does the AI's answer correctly address the "
    "question, matching the reference's substance?\n"
    "   - 5 = directly and correctly answers the question\n"
    "   - 4 = correct but incomplete or imprecise\n"
    "   - 3 = partially correct; misses important elements\n"
    "   - 2 = mostly wrong or misleading\n"
    "   - 1 = irrelevant or fundamentally incorrect\n\n"
    "Submit your scores via the submit_scores tool."
)


# ---------------------------------------------------------------------------
# Loading + DB lookup
# ---------------------------------------------------------------------------


def load_questions(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "questions" not in data:
        raise RuntimeError(f"no `questions` key in {path}")
    questions = data["questions"]
    if not isinstance(questions, list) or not questions:
        raise RuntimeError(f"`questions` in {path} must be a non-empty list")
    seen_ids: set[str] = set()
    for q in questions:
        qid = q.get("id")
        if not qid or not isinstance(qid, str):
            raise RuntimeError(f"question missing id: {q!r}")
        if qid in seen_ids:
            raise RuntimeError(f"duplicate question id: {qid}")
        seen_ids.add(qid)
        if q.get("category") not in CATEGORY_ORDER:
            raise RuntimeError(
                f"{qid}: category must be one of {CATEGORY_ORDER}, got {q.get('category')!r}"
            )
        gold = q.get("gold_stable_ids")
        if not isinstance(gold, list) or not gold:
            raise RuntimeError(f"{qid}: gold_stable_ids must be a non-empty list")
    return questions


def load_generation_gold(path: Path) -> dict[str, str]:
    """Return `{question_id: reference_answer}` for US-036's judge step."""
    with path.open() as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "answers" not in data:
        raise RuntimeError(f"no `answers` key in {path}")
    answers = data["answers"]
    if not isinstance(answers, dict) or not answers:
        raise RuntimeError(f"`answers` in {path} must be a non-empty mapping")
    # Strip trailing whitespace from the literal-block YAML values.
    return {qid: str(ref).strip() for qid, ref in answers.items()}


async def fetch_stable_id_map(database_url: str) -> dict[str, str]:
    """Return `{chunk.id (uuid str): chunk.stable_id}` for all corpus chunks."""
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            "select id, stable_id from public.chunks where stable_id is not null"
        )
    finally:
        await conn.close()
    return {str(r["id"]): r["stable_id"] for r in rows}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def recall_at_k(gold: set[str], retrieved: list[str], k: int) -> float:
    """Per-chunk partial-credit recall: `|gold ∩ top_k| / |gold|`."""
    if not gold:
        return 0.0
    top_k = set(retrieved[:k])
    return len(gold & top_k) / len(gold)


def mrr(gold: set[str], retrieved: list[str]) -> float:
    """1 / rank of first correct chunk in top-10; 0 if none."""
    for i, sid in enumerate(retrieved, start=1):
        if sid in gold:
            return 1.0 / i
    return 0.0


def ndcg_at_5(gold: set[str], retrieved: list[str]) -> float:
    """Binary-relevance nDCG@5 with log2(i+1) position discount."""
    if not gold:
        return 0.0
    dcg = 0.0
    for i, sid in enumerate(retrieved[:5], start=1):
        if sid in gold:
            dcg += 1.0 / math.log2(i + 1)
    # IDCG: max DCG if all gold chunks (capped at 5) were ranked first.
    ideal_hits = min(len(gold), 5)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg > 0 else 0.0


# ---------------------------------------------------------------------------
# Per-mode dispatch (re-uses the real backend functions)
# ---------------------------------------------------------------------------


async def run_query(
    mode: str,
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    question: str,
) -> list[SearchDocumentsResult]:
    if mode == "vector":
        return await search_documents(
            openai_client=openai_client,
            http=http,
            supabase_url=supabase_url,
            supabase_headers=supabase_headers,
            query=question,
            top_k=TOP_K,
        )
    if mode == "keyword":
        return await keyword_only_search(
            openai_client=openai_client,
            http=http,
            supabase_url=supabase_url,
            supabase_headers=supabase_headers,
            query=question,
            top_k=TOP_K,
        )
    if mode == "hybrid":
        return await hybrid_search(
            openai_client=openai_client,
            http=http,
            supabase_url=supabase_url,
            supabase_headers=supabase_headers,
            query=question,
            top_k=TOP_K,
        )
    raise ValueError(f"unknown mode: {mode!r}")


# ---------------------------------------------------------------------------
# Generation + judge (US-036, opt-in via --include-generation)
# ---------------------------------------------------------------------------


async def generate_answer(
    openai_client: AsyncOpenAI,
    question: str,
    context: str,
) -> str:
    """Generate an answer to `question` grounded in `context` via gpt-4o-mini.

    Temperature 0 + fixed seed for determinism. The generator's prompt
    instructs it to use only the provided context, so a high-faithfulness
    score requires retrieval to have surfaced the right chunks — the metric
    pulls double duty as a retrieval-quality signal and a generation-
    quality signal.
    """
    response = await openai_client.chat.completions.create(
        model=GENERATION_MODEL,
        temperature=0,
        seed=GENERATION_SEED,
        max_tokens=GENERATION_MAX_TOKENS,
        messages=[
            {"role": "system", "content": GENERATION_PROMPT_SYSTEM},
            {
                "role": "user",
                "content": GENERATION_PROMPT_USER_TEMPLATE.format(
                    context=context, question=question
                ),
            },
        ],
    )
    return response.choices[0].message.content or ""


# Anthropic SDK is imported lazily so the runner has zero hard dep on it
# when --include-generation is not used. The PR-CI workflow (US-035) does
# not invoke generation; this keeps that path's deps minimal.
_anthropic_module = None


def _get_anthropic():  # type: ignore[no-untyped-def]
    global _anthropic_module
    if _anthropic_module is None:
        try:
            import anthropic  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "--include-generation requires the `anthropic` package. "
                "Run `pip install -r evals/retrieval/requirements.txt`."
            ) from e
        _anthropic_module = anthropic
    return _anthropic_module


JUDGE_TOOL = {
    "name": "submit_scores",
    "description": "Submit integer faithfulness and helpfulness scores (1-5).",
    "input_schema": {
        "type": "object",
        "properties": {
            "faithfulness": {"type": "integer", "minimum": 1, "maximum": 5},
            "helpfulness": {"type": "integer", "minimum": 1, "maximum": 5},
        },
        "required": ["faithfulness", "helpfulness"],
    },
}


async def judge_answer(
    anthropic_client: Any,
    question: str,
    reference: str,
    context: str,
    answer: str,
) -> dict[str, int]:
    """Score the generated answer via Claude. Returns `{faithfulness, helpfulness}`.

    Tool-use rather than freeform JSON guarantees structured output — the
    parsed `input` dict is schema-validated by the API before we see it.
    """
    response = await anthropic_client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        temperature=0,
        tools=[JUDGE_TOOL],
        tool_choice={"type": "tool", "name": "submit_scores"},
        messages=[
            {
                "role": "user",
                "content": JUDGE_PROMPT_TEMPLATE.format(
                    question=question,
                    reference=reference,
                    context=context,
                    answer=answer,
                ),
            }
        ],
    )
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_scores":
            scores = block.input
            return {
                "faithfulness": int(scores["faithfulness"]),
                "helpfulness": int(scores["helpfulness"]),
            }
    raise RuntimeError("judge response did not contain submit_scores tool_use block")


# ---------------------------------------------------------------------------
# Eval loop + aggregation
# ---------------------------------------------------------------------------


async def run_eval(
    questions: list[dict[str, Any]],
    modes: tuple[str, ...],
    stable_id_map: dict[str, str],
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    generation_gold: dict[str, str] | None = None,
    anthropic_client: Any | None = None,
) -> list[dict[str, Any]]:
    """Run retrieval (always) and optionally generation+judge (US-036).

    When `generation_gold` and `anthropic_client` are both provided, for
    each (question × mode) the runner additionally generates an answer
    grounded in the mode's top-`TOP_K_FOR_GENERATION` retrieved chunks and
    has Claude score it for faithfulness + helpfulness.
    """
    include_generation = generation_gold is not None and anthropic_client is not None

    per_question: list[dict[str, Any]] = []
    for q in questions:
        qid = q["id"]
        category = q["category"]
        question = q["question"]
        gold = set(q["gold_stable_ids"])

        entry: dict[str, Any] = {
            "id": qid,
            "category": category,
            "question": question,
            "gold_stable_ids": sorted(gold),
            "by_mode": {},
        }
        if q.get("notes") is not None:
            entry["notes"] = q["notes"]

        reference_answer = (
            generation_gold.get(qid) if include_generation and generation_gold else None
        )

        for mode in modes:
            results = await run_query(
                mode, openai_client, http, supabase_url, supabase_headers, question
            )
            retrieved_stable_ids: list[str] = []
            unknown = 0
            corpus_chunks: list[Any] = []
            for r in results:
                sid = stable_id_map.get(r.id)
                if sid is None:
                    # Non-corpus chunk (would only happen in a mixed-user DB
                    # where another user's upload happens to match). Drop it
                    # from the recall list so the eval measures corpus-only
                    # retrieval, and surface the count for debugging.
                    unknown += 1
                    continue
                retrieved_stable_ids.append(sid)
                corpus_chunks.append(r)

            mode_entry: dict[str, Any] = {
                "top_10_stable_ids": retrieved_stable_ids,
                "mrr": mrr(gold, retrieved_stable_ids),
                "ndcg_at_5": ndcg_at_5(gold, retrieved_stable_ids),
            }
            for k in RECALL_KS:
                mode_entry[f"recall_at_{k}"] = recall_at_k(gold, retrieved_stable_ids, k)
            if unknown:
                mode_entry["unknown_chunks"] = unknown

            if include_generation and reference_answer is not None and corpus_chunks:
                # Context: the mode's top-5 retrieved chunks, concatenated.
                # Doubles as a retrieval-quality signal: if the right chunks
                # aren't here, faithfulness drops because the model has to
                # either hallucinate or refuse.
                context = "\n\n".join(
                    f"[{stable_id_map.get(r.id, r.id)}]\n{r.content}"
                    for r in corpus_chunks[:TOP_K_FOR_GENERATION]
                )
                answer = await generate_answer(openai_client, question, context)
                scores = await judge_answer(
                    anthropic_client,
                    question=question,
                    reference=reference_answer,
                    context=context,
                    answer=answer,
                )
                mode_entry["generated_answer"] = answer
                mode_entry["faithfulness"] = scores["faithfulness"]
                mode_entry["helpfulness"] = scores["helpfulness"]
            elif include_generation and reference_answer is None:
                # The question is in retrieval_gold.yaml but missing from
                # generation_gold.yaml — flag rather than silently drop.
                mode_entry["generation_skipped"] = "no_reference_answer"

            entry["by_mode"][mode] = mode_entry
        per_question.append(entry)
    return per_question


def aggregate(
    per_question: list[dict[str, Any]],
    modes: tuple[str, ...],
) -> dict[str, Any]:
    """Compute per-mode and per-(mode × category) means.

    Retrieval metrics are always present; generation metrics
    (`faithfulness`, `helpfulness`) are included only when every
    (question × mode) cell carries them — partial coverage is reported
    as a per-mode `generation_n` field rather than mean-of-mixed-presence.
    """
    retrieval_keys = ["mrr", "ndcg_at_5"] + [f"recall_at_{k}" for k in RECALL_KS]
    generation_keys = ["faithfulness", "helpfulness"]

    by_mode_sum: dict[str, dict[str, float]] = {
        m: dict.fromkeys(retrieval_keys + generation_keys, 0.0) for m in modes
    }
    by_mode_n: dict[str, int] = dict.fromkeys(modes, 0)
    by_mode_gen_n: dict[str, int] = dict.fromkeys(modes, 0)
    by_mode_category_sum: dict[str, dict[str, dict[str, float]]] = {
        m: defaultdict(lambda: dict.fromkeys(retrieval_keys + generation_keys, 0.0))
        for m in modes
    }
    by_mode_category_n: dict[str, dict[str, int]] = {
        m: defaultdict(int) for m in modes
    }
    by_mode_category_gen_n: dict[str, dict[str, int]] = {
        m: defaultdict(int) for m in modes
    }

    for q in per_question:
        category = q["category"]
        for mode in modes:
            m = q["by_mode"][mode]
            for key in retrieval_keys:
                by_mode_sum[mode][key] += float(m[key])
                by_mode_category_sum[mode][category][key] += float(m[key])
            by_mode_n[mode] += 1
            by_mode_category_n[mode][category] += 1
            if "faithfulness" in m and "helpfulness" in m:
                for key in generation_keys:
                    by_mode_sum[mode][key] += float(m[key])
                    by_mode_category_sum[mode][category][key] += float(m[key])
                by_mode_gen_n[mode] += 1
                by_mode_category_gen_n[mode][category] += 1

    by_mode_mean: dict[str, dict[str, float]] = {}
    for mode in modes:
        n_retr = by_mode_n[mode] or 1
        n_gen = by_mode_gen_n[mode]
        means: dict[str, float] = {}
        for k in retrieval_keys:
            means[k] = round(by_mode_sum[mode][k] / n_retr, 4)
        if n_gen > 0:
            for k in generation_keys:
                means[k] = round(by_mode_sum[mode][k] / n_gen, 4)
            means["generation_n"] = float(n_gen)
        by_mode_mean[mode] = means

    by_mode_category_mean: dict[str, dict[str, dict[str, float]]] = {}
    for mode in modes:
        by_mode_category_mean[mode] = {}
        # Iterate in fixed category order so JSON output is deterministic.
        for category in CATEGORY_ORDER:
            if category in by_mode_category_sum[mode]:
                n_retr = by_mode_category_n[mode][category] or 1
                n_gen = by_mode_category_gen_n[mode][category]
                means_c: dict[str, float] = {}
                for k in retrieval_keys:
                    means_c[k] = round(
                        by_mode_category_sum[mode][category][k] / n_retr, 4
                    )
                if n_gen > 0:
                    for k in generation_keys:
                        means_c[k] = round(
                            by_mode_category_sum[mode][category][k] / n_gen, 4
                        )
                    means_c["generation_n"] = float(n_gen)
                by_mode_category_mean[mode][category] = means_c

    return {
        "by_mode": by_mode_mean,
        "by_mode_category": by_mode_category_mean,
    }


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------


def render_summary(aggregates: dict[str, Any], modes: tuple[str, ...]) -> str:
    """Two-or-three markdown tables wrapped in EVAL_SUMMARY markers.

    The third table (generation quality) renders only when at least one
    mode carries `faithfulness` / `helpfulness` from US-036's
    `--include-generation` path.
    """
    by_mode = aggregates["by_mode"]
    has_generation = any("faithfulness" in by_mode[m] for m in modes)

    lines: list[str] = [
        "<!-- BEGIN EVAL_SUMMARY -->",
        "",
        "### Headline (mean across 50 questions)",
        "",
        "| Mode | recall@5 | MRR | nDCG@5 |",
        "|---|---|---|---|",
    ]
    for mode in modes:
        m = by_mode[mode]
        lines.append(
            f"| {mode} | {m['recall_at_5']:.3f} | {m['mrr']:.3f} | {m['ndcg_at_5']:.3f} |"
        )
    lines += [
        "",
        "### Per-category breakdown",
        "",
        "| Mode | Category | recall@5 | MRR |",
        "|---|---|---|---|",
    ]
    for mode in modes:
        cat_map = aggregates["by_mode_category"].get(mode, {})
        for category in CATEGORY_ORDER:
            if category in cat_map:
                m = cat_map[category]
                lines.append(
                    f"| {mode} | {category} | {m['recall_at_5']:.3f} | {m['mrr']:.3f} |"
                )

    if has_generation:
        lines += [
            "",
            "### Generation quality (LLM judge — Claude on gpt-4o-mini answers)",
            "",
            "| Mode | n | Faithfulness (1-5) | Helpfulness (1-5) |",
            "|---|---|---|---|",
        ]
        for mode in modes:
            m = by_mode[mode]
            if "faithfulness" not in m:
                continue
            n = int(m.get("generation_n", 0))
            lines.append(
                f"| {mode} | {n} | {m['faithfulness']:.2f} | {m['helpfulness']:.2f} |"
            )

    lines += ["", "<!-- END EVAL_SUMMARY -->", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def amain() -> int:
    parser = argparse.ArgumentParser(description="US-033 retrieval eval runner")
    parser.add_argument(
        "--questions", type=Path, default=DEFAULT_QUESTIONS,
        help="Path to retrieval_gold.yaml",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="JSON output path; default: results/<ISO-timestamp>.json",
    )
    parser.add_argument(
        "--summary", type=Path, default=DEFAULT_SUMMARY,
        help="Markdown summary path; default: evals/retrieval/summary.md",
    )
    parser.add_argument(
        "--mode",
        choices=["vector", "keyword", "hybrid", "all"],
        default="all",
        help="Run a single retrieval mode or all three (default).",
    )
    parser.add_argument(
        "--include-generation",
        action="store_true",
        help=(
            "US-036: additionally generate an answer per (question × mode) "
            "from the mode's top-5 retrieved chunks, then have Claude score "
            "the answer for faithfulness + helpfulness. Requires "
            "ANTHROPIC_API_KEY and the `anthropic` package."
        ),
    )
    parser.add_argument(
        "--generation-gold",
        type=Path,
        default=DEFAULT_GENERATION_GOLD,
        help="Path to generation_gold.yaml (reference answers).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    supabase_url = os.environ.get("SUPABASE_URL")
    if not supabase_url:
        raise RuntimeError("SUPABASE_URL is required")
    service_role_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_SERVICE_ROLE")
    )
    if not service_role_key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is required — eval bypasses RLS so it "
            "can read the corpus chunks owned by the seed user"
        )
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required (vector + hybrid embed the query)")
    database_url = (
        os.environ.get("CORPUS_SEED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not database_url:
        raise RuntimeError("CORPUS_SEED_DATABASE_URL or DATABASE_URL is required")

    modes: tuple[str, ...] = MODES if args.mode == "all" else (args.mode,)
    questions = load_questions(args.questions)

    generation_gold: dict[str, str] | None = None
    anthropic_client: Any | None = None
    if args.include_generation:
        anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not anthropic_api_key:
            raise RuntimeError(
                "--include-generation requires ANTHROPIC_API_KEY "
                "(the Claude judge cannot use the OpenAI key)"
            )
        generation_gold = load_generation_gold(args.generation_gold)
        missing = [q["id"] for q in questions if q["id"] not in generation_gold]
        if missing:
            log.warning(
                "generation_gold.yaml missing reference answers for %d questions: %s",
                len(missing),
                ", ".join(missing[:5]) + ("…" if len(missing) > 5 else ""),
            )
        anthropic_mod = _get_anthropic()
        anthropic_client = anthropic_mod.AsyncAnthropic(api_key=anthropic_api_key)

    stable_id_map = await fetch_stable_id_map(database_url)
    if not stable_id_map:
        raise RuntimeError(
            "no chunks with stable_id found — run `python -m db_seed.corpus_seed` first"
        )

    supabase_headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }

    openai_client = AsyncOpenAI(api_key=openai_api_key)
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=30.0) as http:
        per_question = await run_eval(
            questions,
            modes,
            stable_id_map,
            openai_client,
            http,
            supabase_url,
            supabase_headers,
            generation_gold=generation_gold,
            anthropic_client=anthropic_client,
        )

    aggregates = aggregate(per_question, modes)
    elapsed_s = round(time.perf_counter() - started, 2)

    results = {
        "generated_at": started_at,
        "elapsed_s": elapsed_s,
        "modes": list(modes),
        "n_questions": len(per_question),
        "n_corpus_chunks": len(stable_id_map),
        "generation_included": bool(args.include_generation),
        "generation_model": GENERATION_MODEL if args.include_generation else None,
        "judge_model": JUDGE_MODEL if args.include_generation else None,
        "per_question": per_question,
        "aggregates": aggregates,
    }

    out_path = args.out
    if out_path is None:
        DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = started_at.replace(":", "").replace("-", "")
        out_path = DEFAULT_RESULTS_DIR / f"{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    args.summary.write_text(render_summary(aggregates, modes), encoding="utf-8")

    suffix = " + generation" if args.include_generation else ""
    print(
        f"retrieval eval done{suffix}: {len(per_question)} questions × "
        f"{len(modes)} modes in {elapsed_s}s → {out_path}"
    )
    for mode in modes:
        m = aggregates["by_mode"][mode]
        line = (
            f"  {mode:>8}: recall@5={m['recall_at_5']:.3f} "
            f"mrr={m['mrr']:.3f} ndcg@5={m['ndcg_at_5']:.3f}"
        )
        if "faithfulness" in m:
            line += (
                f" | faith={m['faithfulness']:.2f} help={m['helpfulness']:.2f}"
                f" (n={int(m['generation_n'])})"
            )
        print(line)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
