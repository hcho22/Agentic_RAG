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
import random
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import asyncpg
import httpx
import jwt as pyjwt
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

from .e6 import (  # noqa: E402
    E6_VIEWER_EMAIL,
    E6_VIEWER_ID,
    E6Result,
    render_e6_section,
    run_e6,
)
from .ragas import (  # noqa: E402
    RAGAS_CELL_IDS,
    RAGAS_JUDGE_MODEL,
    RAGAS_METRICS,
    build_ragas_section,
    ragas_cell_enabled,
    score_with_ragas,
)
from .ragas_gates import (  # noqa: E402
    GateFinding,
    check_diagnostic_gates,
    check_operational_gates,
    check_score_regressions,
    load_custom_judge_history,
    load_ragas_history,
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

# US-042: viewer parameterization. The runner replays each question under
# three permission setups and two filter strategies so the eval can prove:
#   * SECURITY     — pre-filter SQL never returns gold to no_access viewers
#   * RECALL       — post-filter (no SQL filter, drop in Python) collapses
#                    recall as permissions sparsen; pre-filter does not
#   * NON-REGRESS  — under full_access the pre-filter behaves identically
#                    to the pre-Module-11 baseline
ViewerKind = Literal["full_access", "partial_access", "no_access"]
FilterStrategy = Literal["pre_filter", "post_filter"]
VIEWER_ORDER: tuple[ViewerKind, ...] = ("full_access", "partial_access", "no_access")
FILTER_ORDER: tuple[FilterStrategy, ...] = ("pre_filter", "post_filter")

# Corpus chunks are owned by this sentinel user (db_seed.corpus_seed.CORPUS_USER_ID).
# Duplicated here rather than imported so the runner stays decoupled from
# the seeder when it runs in CI (the corpus may be seeded by a different
# code path, e.g. fixtures).
CORPUS_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
CORPUS_USER_EMAIL = "corpus-seed@local.test"

# Two persistent test viewers — UUID5 keeps them stable across runs so the
# auth.users + chunk_acl rows can be upserted idempotently without
# accumulating cruft. Their visible-chunks set is reset per question.
EVAL_VIEWER_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000000042")
PARTIAL_VIEWER_ID = uuid.uuid5(EVAL_VIEWER_NAMESPACE, "eval-viewer-partial-access")
NO_ACCESS_VIEWER_ID = uuid.uuid5(EVAL_VIEWER_NAMESPACE, "eval-viewer-no-access")
PARTIAL_VIEWER_EMAIL = "eval-partial@local.test"
NO_ACCESS_VIEWER_EMAIL = "eval-no-access@local.test"

# US-002: the fixed Default Workspace the corpus + eval viewers live in. Must
# match the init migration (20260617120200_default_workspace_backfill.sql) and
# db_seed.corpus_seed.DEFAULT_WORKSPACE_ID. These users are created here, after
# the migration's auth.users backfill runs, so ensure_viewer_users adds their
# membership explicitly — otherwise the US-003 subtractive membership clause
# would hide the corpus and E4/E6 would regress to all-zero recall.
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")

# Local-dev default; production / CI overrides via SUPABASE_JWT_SECRET.
LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"

# Non-regression baseline (full_access × pre_filter recall@5). Frozen at the
# numbers produced by full_access × pre_filter on a clean US-042 run with
# the .env-default SEARCH_SIMILARITY_THRESHOLD=0.4 (the threshold CI uses).
# The pre-Module-11 summary.md (vector 0.860 / hybrid 0.860) was generated
# under SEARCH_SIMILARITY_THRESHOLD=0.3 — different threshold, different
# numbers, not a regression — so those values would falsely flag drift on
# every PR. Rebaselining at 0.4 keeps the test apples-to-apples with the
# environment the eval actually runs in. Bump these numbers when the
# corpus, chunker, threshold, or embedding model legitimately shifts and
# the new baseline is the agreed-upon truth going forward.
MODULE_10_BASELINE_RECALL_AT_5: dict[str, float] = {
    "vector": 0.670,
    "keyword": 0.110,
    "hybrid": 0.670,
}
NON_REGRESSION_TOLERANCE = 0.005

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


def load_questions(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (questions, viewer_construction).

    `viewer_construction` is an empty dict when the YAML lacks the block
    (legacy format); the runner then falls back to full_access only.
    """
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
    viewer_construction = data.get("viewer_construction", {}) or {}
    return questions, viewer_construction


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
# US-042: viewer construction + setup
# ---------------------------------------------------------------------------


def compute_visible_stable_ids(
    viewer: ViewerKind,
    question: dict[str, Any],
    all_corpus_stable_ids: list[str],
    construction: dict[str, Any],
) -> set[str]:
    """Compute the visible-chunks set for a (question × viewer) pair.

    Pure / deterministic. The random choice in `partial_access` is seeded
    by `question["id"]` so two runs of the eval produce identical visible
    sets per question. The construction rules come from the YAML's
    `viewer_construction` block — keeping them out of code makes the YAML
    the audit trail for what each viewer setup was allowed to see.
    """
    gold = set(question["gold_stable_ids"])
    qid = question["id"]
    if viewer == "full_access":
        return set(all_corpus_stable_ids)
    if viewer == "partial_access":
        cfg = construction.get("partial_access") or {}
        n_extra = int(cfg.get("n_extra_chunks", 0))
        non_gold = sorted(set(all_corpus_stable_ids) - gold)
        rng = random.Random(qid)
        sample = set(rng.sample(non_gold, min(n_extra, len(non_gold))))
        return gold | sample
    if viewer == "no_access":
        return set(all_corpus_stable_ids) - gold
    raise ValueError(f"unknown viewer: {viewer!r}")


def mint_user_jwt(user_id: uuid.UUID, email: str, secret: str) -> str:
    """HS256 JWT shaped like a Supabase auth token (sub, role, aud, exp).

    Long expiry (1 day) so the same JWT is reused across the entire run.
    """
    now = int(time.time())
    payload = {
        "iss": "agentic-rag-eval",
        "sub": str(user_id),
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
        "iat": now,
        "exp": now + 86400,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


def user_headers(jwt_token: str, anon_or_service_key: str) -> dict[str, str]:
    """PostgREST headers for a user-JWT request.

    The `apikey` header is the project's anon (or service) key — required
    by Supabase's edge router; PostgREST itself looks at Authorization to
    set the role.
    """
    return {
        "apikey": anon_or_service_key,
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }


async def ensure_viewer_users(database_url: str) -> None:
    """Idempotently insert the corpus user + the two eval viewers, and make all
    three members of the Default Workspace.

    Direct insert into auth.users bypasses the normal Supabase Auth flow;
    only acceptable here because the runner is a service-role-equipped
    eval against a local / CI database.

    US-002: these users are created after the init migration's auth.users
    backfill, so their Default-Workspace memberships are added here. Without
    them the US-003 subtractive membership clause hides the corpus and recall
    collapses to zero for every viewer.
    """
    conn = await asyncpg.connect(database_url)
    try:
        await conn.executemany(
            """
            insert into auth.users (
                id, instance_id, aud, role, email, encrypted_password,
                email_confirmed_at, raw_app_meta_data, raw_user_meta_data,
                created_at, updated_at
            ) values (
                $1, '00000000-0000-0000-0000-000000000000',
                'authenticated', 'authenticated', $2, '',
                now(),
                '{"provider":"eval","providers":["eval"]}'::jsonb,
                '{}'::jsonb,
                now(), now()
            )
            on conflict (id) do nothing
            """,
            [
                (CORPUS_USER_ID, CORPUS_USER_EMAIL),
                (PARTIAL_VIEWER_ID, PARTIAL_VIEWER_EMAIL),
                (NO_ACCESS_VIEWER_ID, NO_ACCESS_VIEWER_EMAIL),
            ],
        )
        await conn.executemany(
            """
            insert into public.workspace_membership (workspace_id, user_id, role)
            values ($1, $2, 'member')
            on conflict do nothing
            """,
            [
                (DEFAULT_WORKSPACE_ID, CORPUS_USER_ID),
                (DEFAULT_WORKSPACE_ID, PARTIAL_VIEWER_ID),
                (DEFAULT_WORKSPACE_ID, NO_ACCESS_VIEWER_ID),
            ],
        )
    finally:
        await conn.close()


async def reset_viewer_acls(
    conn: asyncpg.Connection,
    visible_chunk_ids: dict[ViewerKind, set[uuid.UUID]],
) -> None:
    """Per-question ACL replacement for the two non-full viewers.

    Single transaction: delete all chunk_acl rows owned by the test
    viewers, then bulk-insert the new visible-set. ~50 DELETE + ~50
    INSERTs per question is well under the budget.
    """
    async with conn.transaction():
        await conn.execute(
            """
            delete from public.chunk_acl
             where principal_type = 'user'
               and principal_id = any($1::uuid[])
            """,
            [PARTIAL_VIEWER_ID, NO_ACCESS_VIEWER_ID],
        )
        rows: list[tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]] = []
        for viewer_id, ids in (
            (PARTIAL_VIEWER_ID, visible_chunk_ids["partial_access"]),
            (NO_ACCESS_VIEWER_ID, visible_chunk_ids["no_access"]),
        ):
            for cid in ids:
                rows.append((cid, "user", viewer_id, CORPUS_USER_ID))
        if rows:
            await conn.executemany(
                """
                insert into public.chunk_acl
                  (chunk_id, principal_type, principal_id, granted_by)
                values ($1, $2, $3, $4)
                on conflict do nothing
                """,
                rows,
            )


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


def _metrics_block(
    gold: set[str],
    retrieved_stable_ids: list[str],
    unknown: int,
) -> dict[str, Any]:
    """Build the canonical metrics dict for one (question × mode × viewer × filter)."""
    block: dict[str, Any] = {
        "top_10_stable_ids": retrieved_stable_ids,
        "mrr": mrr(gold, retrieved_stable_ids),
        "ndcg_at_5": ndcg_at_5(gold, retrieved_stable_ids),
    }
    for k in RECALL_KS:
        block[f"recall_at_{k}"] = recall_at_k(gold, retrieved_stable_ids, k)
    if unknown:
        block["unknown_chunks"] = unknown
    return block


def _project_to_corpus(
    results: list[Any],
    stable_id_map: dict[str, str],
    visible_set: set[str] | None,
) -> tuple[list[str], list[Any], int]:
    """Map RPC rows to (retrieved_stable_ids, corpus_chunks, unknown_count).

    `visible_set` is the post-filter visible-chunks set in stable_id form;
    when set, chunks not in it are dropped *after* retrieval (post-filter
    semantics). When None, no post-filter drop happens.
    """
    retrieved: list[str] = []
    corpus_chunks: list[Any] = []
    unknown = 0
    for r in results:
        sid = stable_id_map.get(r.id)
        if sid is None:
            unknown += 1
            continue
        if visible_set is not None and sid not in visible_set:
            continue
        retrieved.append(sid)
        corpus_chunks.append(r)
    return retrieved, corpus_chunks, unknown


def _format_generation_context(
    chunks: list[Any], stable_id_map: dict[str, str]
) -> str:
    """Render retrieved chunks into the stable-id-labelled context block fed to the generator."""
    return "\n\n".join(
        f"[{stable_id_map.get(r.id, r.id)}]\n{r.content}" for r in chunks
    )


async def run_eval(
    questions: list[dict[str, Any]],
    modes: tuple[str, ...],
    viewers: tuple[ViewerKind, ...],
    viewer_construction: dict[str, Any],
    viewer_headers: dict[ViewerKind, dict[str, str]],
    stable_id_map: dict[str, str],
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    owner_headers: dict[str, str],
    db_conn: asyncpg.Connection | None,
    generation_gold: dict[str, str] | None = None,
    anthropic_client: Any | None = None,
    include_ragas: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run retrieval per (question × mode × viewer × filter_strategy).

    Every (mode × viewer) cell carries both `pre_filter` and `post_filter`
    metric blocks. Pre-filter calls match_chunks under the viewer's own JWT
    (so the SQL permission predicate runs against the viewer's chunk_acl
    rows). Post-filter calls match_chunks under the corpus owner's JWT
    (sees everything) and then drops chunks not in the viewer's visible set
    in Python — that's the "no SQL filter, drop in Python" baseline the
    Recall trade-off table compares against.

    Generation + judge (US-036) runs only for full_access × pre_filter to
    keep the cost bounded; that's the canonical "pretend permissions don't
    exist" baseline the generation table is meant to characterise.

    When `include_ragas` is set, the second return value collects one RAGAS
    input row per gated cell (`ragas_cell_enabled` — hybrid × the two
    pre_filter cells). full_access reuses the answer the generation block
    already produced; partial_access generates one here. The Claude judge
    stays full_access-only, so partial_access carries an answer but no judge
    scores. The list is empty when `include_ragas` is False.

    `db_conn` may be None when only `full_access` is requested — the per-
    question chunk_acl reset is then unnecessary. The function asserts the
    invariant.
    """
    include_generation = generation_gold is not None and anthropic_client is not None
    needs_db = any(v != "full_access" for v in viewers)
    if needs_db and db_conn is None:
        raise RuntimeError(
            "db_conn is required when partial_access / no_access viewers are enabled"
        )

    all_corpus_stable_ids = sorted(stable_id_map.values())
    sid_to_chunk_id: dict[str, uuid.UUID] = {
        sid: uuid.UUID(cid) for cid, sid in stable_id_map.items()
    }

    per_question: list[dict[str, Any]] = []
    ragas_rows: list[dict[str, Any]] = []
    for q in questions:
        qid = q["id"]
        category = q["category"]
        question = q["question"]
        gold = set(q["gold_stable_ids"])

        # Compute visible sets for the non-full viewers and reset chunk_acl.
        visible_stable: dict[ViewerKind, set[str]] = {}
        for viewer in viewers:
            visible_stable[viewer] = compute_visible_stable_ids(
                viewer, q, all_corpus_stable_ids, viewer_construction
            )
        if needs_db and db_conn is not None:
            visible_chunk_ids: dict[ViewerKind, set[uuid.UUID]] = {
                "partial_access": {
                    sid_to_chunk_id[sid]
                    for sid in visible_stable.get("partial_access", set())
                    if sid in sid_to_chunk_id
                },
                "no_access": {
                    sid_to_chunk_id[sid]
                    for sid in visible_stable.get("no_access", set())
                    if sid in sid_to_chunk_id
                },
            }
            await reset_viewer_acls(db_conn, visible_chunk_ids)

        entry: dict[str, Any] = {
            "id": qid,
            "category": category,
            "question": question,
            "gold_stable_ids": sorted(gold),
            "by_mode": {},
            "by_viewer": {},
        }
        if q.get("notes") is not None:
            entry["notes"] = q["notes"]

        reference_answer = (
            generation_gold.get(qid) if include_generation and generation_gold else None
        )

        for mode in modes:
            # Owner-side retrieval is the single source of truth for the
            # post-filter ranking across all viewers — fetch it once per
            # mode and re-project per viewer rather than calling N times.
            owner_results = await run_query(
                mode, openai_client, http, supabase_url, owner_headers, question
            )

            mode_by_viewer: dict[str, dict[str, Any]] = {}

            for viewer in viewers:
                viewer_set = visible_stable[viewer]

                # Post-filter: same owner ranking, drop chunks not visible.
                post_ids, _post_chunks, post_unknown = _project_to_corpus(
                    owner_results, stable_id_map, viewer_set
                )
                post_block = _metrics_block(gold, post_ids, post_unknown)

                # Pre-filter: query as the viewer themselves. For
                # full_access we re-use the owner ranking — same JWT.
                if viewer == "full_access":
                    pre_results = owner_results
                else:
                    pre_results = await run_query(
                        mode,
                        openai_client,
                        http,
                        supabase_url,
                        viewer_headers[viewer],
                        question,
                    )
                pre_ids, pre_corpus_chunks, pre_unknown = _project_to_corpus(
                    pre_results, stable_id_map, None
                )
                pre_block = _metrics_block(gold, pre_ids, pre_unknown)

                # Generation + judge runs only on the canonical cell so
                # the cost stays at the US-036 budget regardless of how
                # many viewers are exercised.
                if (
                    viewer == "full_access"
                    and include_generation
                    and reference_answer is not None
                    and pre_corpus_chunks
                ):
                    context = _format_generation_context(
                        pre_corpus_chunks[:TOP_K_FOR_GENERATION], stable_id_map
                    )
                    answer = await generate_answer(openai_client, question, context)
                    scores = await judge_answer(
                        anthropic_client,
                        question=question,
                        reference=reference_answer,
                        context=context,
                        answer=answer,
                    )
                    pre_block["generated_answer"] = answer
                    pre_block["faithfulness"] = scores["faithfulness"]
                    pre_block["helpfulness"] = scores["helpfulness"]
                elif (
                    viewer == "full_access"
                    and include_generation
                    and reference_answer is None
                ):
                    pre_block["generation_skipped"] = "no_reference_answer"

                # RAGAS input rows are collected only for the gated cells
                # (hybrid × the two pre_filter cells). full_access reuses the
                # answer the generation block above already produced;
                # partial_access generates one here so RAGAS has an answer to
                # score — without invoking the Claude judge, which stays
                # full_access-only (the US-036 table's scope is unchanged).
                if (
                    include_ragas
                    and ragas_cell_enabled(mode, viewer, "pre_filter")
                    and reference_answer is not None
                    and pre_corpus_chunks
                ):
                    ragas_chunks = pre_corpus_chunks[:TOP_K_FOR_GENERATION]
                    ragas_answer = pre_block.get("generated_answer")
                    if ragas_answer is None:
                        ragas_context = _format_generation_context(
                            ragas_chunks, stable_id_map
                        )
                        ragas_answer = await generate_answer(
                            openai_client, question, ragas_context
                        )
                    ragas_rows.append(
                        {
                            "question_id": qid,
                            "cell": f"{viewer}:pre_filter",
                            "mode": mode,
                            "question": question,
                            "contexts": [r.content for r in ragas_chunks],
                            "answer": ragas_answer,
                            "reference": reference_answer,
                        }
                    )

                mode_by_viewer[viewer] = {
                    "pre_filter": pre_block,
                    "post_filter": post_block,
                }

            # Backward-compat: the canonical full_access × pre_filter cell
            # remains accessible at entry["by_mode"][mode] so US-035's
            # delta workflow and US-036 generation downstream don't have
            # to learn the new shape.
            if "full_access" in mode_by_viewer:
                entry["by_mode"][mode] = mode_by_viewer["full_access"]["pre_filter"]
            for viewer in viewers:
                entry["by_viewer"].setdefault(viewer, {})[mode] = mode_by_viewer[viewer]

        per_question.append(entry)
    return per_question, ragas_rows


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

    aggregates_out: dict[str, Any] = {
        "by_mode": by_mode_mean,
        "by_mode_category": by_mode_category_mean,
    }
    aggregates_out.update(_aggregate_viewer_filter(per_question, modes))
    return aggregates_out


def _aggregate_viewer_filter(
    per_question: list[dict[str, Any]],
    modes: tuple[str, ...],
) -> dict[str, Any]:
    """Compute the three new US-042 aggregates from per_question["by_viewer"].

    Returns four keys:
      * `by_viewer_filter`     — full mode×viewer×filter mean table
                                 (recall@5, MRR, nDCG@5).
      * `security_no_access`   — fraction of no_access runs that returned
                                 ZERO gold chunks (per mode × filter).
                                 Pre-filter must be 1.0; post-filter is
                                 also 1.0 for this construction (the
                                 visible_set excludes gold so the Python
                                 drop achieves the same), but the table
                                 keeps both for symmetry.
      * `recall_tradeoff`      — per mode × category, partial_access
                                 recall@5 pre-filter vs post-filter.
      * `non_regression`       — per mode, full_access × pre-filter
                                 recall@5 vs MODULE_10_BASELINE_RECALL_AT_5,
                                 plus a `delta` and a `within_tolerance`
                                 boolean (|delta| ≤ NON_REGRESSION_TOLERANCE).
    """
    keys = ("recall_at_5", "mrr", "ndcg_at_5")

    by_viewer_filter: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for viewer in VIEWER_ORDER:
        viewer_present = any(viewer in q.get("by_viewer", {}) for q in per_question)
        if not viewer_present:
            continue
        by_viewer_filter[viewer] = {}
        for filt in FILTER_ORDER:
            by_viewer_filter[viewer][filt] = {}
            for mode in modes:
                sums = dict.fromkeys(keys, 0.0)
                n = 0
                for q in per_question:
                    cell = q.get("by_viewer", {}).get(viewer, {}).get(mode)
                    if cell is None:
                        continue
                    block = cell.get(filt)
                    if block is None:
                        continue
                    for k in keys:
                        sums[k] += float(block[k])
                    n += 1
                if n > 0:
                    by_viewer_filter[viewer][filt][mode] = {
                        k: round(sums[k] / n, 4) for k in keys
                    } | {"n": float(n)}

    security_no_access: dict[str, dict[str, float]] = {}
    if any("no_access" in q.get("by_viewer", {}) for q in per_question):
        for filt in FILTER_ORDER:
            security_no_access[filt] = {}
            for mode in modes:
                no_gold_runs = 0
                total = 0
                for q in per_question:
                    block = (
                        q.get("by_viewer", {})
                        .get("no_access", {})
                        .get(mode, {})
                        .get(filt)
                    )
                    if block is None:
                        continue
                    total += 1
                    # "0 gold chunks retrieved" — recall@k=0 across the board
                    # is the simplest signal. Use recall_at_10 (widest k) so
                    # the test catches leakage anywhere in the top-10.
                    if float(block.get("recall_at_10", 0.0)) == 0.0:
                        no_gold_runs += 1
                if total > 0:
                    security_no_access[filt][mode] = round(no_gold_runs / total, 4)

    recall_tradeoff: dict[str, dict[str, dict[str, float]]] = {}
    if any("partial_access" in q.get("by_viewer", {}) for q in per_question):
        recall_tradeoff = {}
        # Per (mode × category): mean recall@5 under pre vs post.
        for mode in modes:
            recall_tradeoff[mode] = {}
            for category in (None,) + CATEGORY_ORDER:  # None = overall
                pre_sum = 0.0
                post_sum = 0.0
                n = 0
                for q in per_question:
                    if category is not None and q["category"] != category:
                        continue
                    cell = q.get("by_viewer", {}).get("partial_access", {}).get(mode)
                    if cell is None:
                        continue
                    pre_sum += float(cell["pre_filter"]["recall_at_5"])
                    post_sum += float(cell["post_filter"]["recall_at_5"])
                    n += 1
                if n > 0:
                    label = "overall" if category is None else category
                    recall_tradeoff[mode][label] = {
                        "pre_filter": round(pre_sum / n, 4),
                        "post_filter": round(post_sum / n, 4),
                        "delta": round((pre_sum - post_sum) / n, 4),
                        "n": float(n),
                    }

    non_regression: dict[str, dict[str, float | bool]] = {}
    if any("full_access" in q.get("by_viewer", {}) for q in per_question):
        for mode in modes:
            full_pre = (
                by_viewer_filter.get("full_access", {})
                .get("pre_filter", {})
                .get(mode)
            )
            if full_pre is None:
                continue
            actual = float(full_pre["recall_at_5"])
            baseline = MODULE_10_BASELINE_RECALL_AT_5.get(mode)
            if baseline is None:
                continue
            delta = round(actual - baseline, 4)
            non_regression[mode] = {
                "actual_recall_at_5": actual,
                "baseline_recall_at_5": baseline,
                "delta": delta,
                "tolerance": NON_REGRESSION_TOLERANCE,
                "within_tolerance": abs(delta) <= NON_REGRESSION_TOLERANCE,
            }

    return {
        "by_viewer_filter": by_viewer_filter,
        "security_no_access": security_no_access,
        "recall_tradeoff": recall_tradeoff,
        "non_regression": non_regression,
    }


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------


def render_summary(
    aggregates: dict[str, Any],
    modes: tuple[str, ...],
    ragas_section: dict[str, Any] | None = None,
    diagnostic_findings: list[GateFinding] | None = None,
    e6_result: E6Result | None = None,
) -> str:
    """Markdown tables wrapped in EVAL_SUMMARY markers.

    The generation-quality table renders only when at least one mode carries
    `faithfulness` / `helpfulness` from US-036's `--include-generation` path.
    The RAGAS comparison table (US-004) is always rendered — its inner
    `EVAL_SUMMARY_RAGAS` markers are emitted even on runs without
    `--include-ragas` (body shows a placeholder), so the `docs/evals.md`
    embed target never goes stale. The `Diagnostics` section (US-006) renders
    only when `diagnostic_findings` is non-empty — a clean run, or an
    early-rollout run without enough history, shows no Diagnostics section.
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

    # RAGAS comparison table (US-004). Always rendered. The EVAL_SUMMARY_RAGAS
    # markers let docs/_embed_eval_summaries.py lift just this table into
    # docs/evals.md; the `### RAGAS comparison` heading sits outside them so
    # the embed target supplies its own framing without a doubled heading.
    lines += [
        "",
        "### RAGAS comparison",
        "",
        "<!-- EVAL_SUMMARY_RAGAS_START -->",
        "",
    ]
    if ragas_section is None:
        lines.append(
            "_(RAGAS not run on this snapshot — pass --include-ragas to enable)_"
        )
    else:
        by_cell = ragas_section.get("aggregates", {}).get("by_cell", {})
        lines += [
            "| Metric | Cell | mean_strict | mean_available | Coverage | API errors |",
            "|---|---|---|---|---|---|",
        ]
        for metric in RAGAS_METRICS:
            metric_label = metric.replace("_", " ").title()
            for cell_id in RAGAS_CELL_IDS:
                block = by_cell.get(cell_id, {}).get(metric)
                if block is None:
                    lines.append(f"| {metric_label} | {cell_id} | — | — | — | — |")
                    continue
                available = block["mean_available"]
                available_s = f"{available:.3f}" if available is not None else "—"
                lines.append(
                    f"| {metric_label} | {cell_id} | {block['mean_strict']:.3f} | "
                    f"{available_s} | {block['coverage']:.3f} | {block['api_errors']} |"
                )
    lines += ["", "<!-- EVAL_SUMMARY_RAGAS_END -->"]

    # US-006: yellow diagnostic findings (coverage / api_error drift), as a
    # markdown list. Sits right after the RAGAS comparison table; renders only
    # when there are findings, so a clean run keeps the compact summary.
    if diagnostic_findings:
        lines += ["", "### Diagnostics", ""]
        for finding in diagnostic_findings:
            lines.append(f"- **{finding.tag}** — {finding.message}")

    # US-042: viewer-parameterized tables. Each renders only when its
    # source aggregate is non-empty so retrieval-only / single-viewer
    # invocations keep producing the same compact summary as before.
    security = aggregates.get("security_no_access", {})
    if security:
        lines += [
            "",
            "### Security (US-042) — fraction of no_access runs that returned 0 gold chunks",
            "",
            "| Mode | Pre-filter | Post-filter |",
            "|---|---|---|",
        ]
        for mode in modes:
            pre = security.get("pre_filter", {}).get(mode)
            post = security.get("post_filter", {}).get(mode)
            if pre is None and post is None:
                continue
            pre_s = f"{pre:.3f}" if pre is not None else "—"
            post_s = f"{post:.3f}" if post is not None else "—"
            lines.append(f"| {mode} | {pre_s} | {post_s} |")

    tradeoff = aggregates.get("recall_tradeoff", {})
    if tradeoff:
        lines += [
            "",
            "### Recall trade-off (US-042) — partial_access recall@5: pre-filter vs post-filter",
            "",
            "| Mode | Category | Pre | Post | Δ (pre−post) |",
            "|---|---|---|---|---|",
        ]
        for mode in modes:
            cat_map = tradeoff.get(mode, {})
            row_order = ["overall"] + list(CATEGORY_ORDER)
            for label in row_order:
                cell = cat_map.get(label)
                if cell is None:
                    continue
                lines.append(
                    f"| {mode} | {label} | {cell['pre_filter']:.3f} | "
                    f"{cell['post_filter']:.3f} | {cell['delta']:+.3f} |"
                )

    non_reg = aggregates.get("non_regression", {})
    if non_reg:
        lines += [
            "",
            "### Non-regression (US-042) — full_access recall@5 vs Module-10 baseline",
            "",
            "| Mode | Actual | Baseline | Δ | Within ±0.005? |",
            "|---|---|---|---|---|",
        ]
        for mode in modes:
            cell = non_reg.get(mode)
            if cell is None:
                continue
            ok = "✓" if cell["within_tolerance"] else "✗"
            lines.append(
                f"| {mode} | {cell['actual_recall_at_5']:.3f} | "
                f"{cell['baseline_recall_at_5']:.3f} | "
                f"{cell['delta']:+.3f} | {ok} |"
            )

    # US-009: E6 second-workspace zero-leak block. Renders only when E6 ran, so
    # the default (E4-only) summary is byte-identical to before.
    if e6_result is not None:
        lines += render_e6_section(e6_result)

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
    parser.add_argument(
        "--viewers",
        choices=["full", "partial", "no_access", "all"],
        default="all",
        help=(
            "US-042: viewer setups to run. `all` (default) walks the three "
            "permission setups defined in the YAML's viewer_construction "
            "block — 50 questions × 3 viewers per mode. `full` reproduces "
            "the pre-Module-11 single-viewer behaviour."
        ),
    )
    parser.add_argument(
        "--include-ragas",
        action="store_true",
        help=(
            "RAGAS integration: additionally score the hybrid-mode "
            "full_access / partial_access pre_filter cells with the four "
            "canonical RAGAS metrics (Faithfulness, Answer Relevancy, Context "
            "Precision, Context Recall). Implies --include-generation (RAGAS "
            "needs generated answers) and requires the `ragas` package."
        ),
    )
    parser.add_argument(
        "--include-e6",
        action="store_true",
        help=(
            "US-009: additionally run the E6 second-workspace zero-leak eval. "
            "Seeds a second Workspace B (a copy of the gold corpus) and asserts "
            "a cross-workspace viewer retrieves 0 of B's gold under every mode + "
            "filter, with a positive control proving B's gold is detectable. "
            "Additive to the E4 sweep; a leak (or a blind positive control) "
            "fails the run (exit 1) — this is a pinned security invariant."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # --include-ragas needs generated answers to score, so it auto-enables
    # --include-generation rather than erroring when the operator omits it.
    if args.include_ragas and not args.include_generation:
        log.info(
            "auto-enabling --include-generation because --include-ragas "
            "requires generated answers"
        )
        args.include_generation = True

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
    questions, viewer_construction = load_questions(args.questions)

    viewer_alias = {
        "full": ("full_access",),
        "partial": ("full_access", "partial_access"),
        "no_access": ("full_access", "no_access"),
        "all": VIEWER_ORDER,
    }
    viewers: tuple[ViewerKind, ...] = viewer_alias[args.viewers]
    if not viewer_construction and any(v != "full_access" for v in viewers):
        raise RuntimeError(
            "non-full viewers requested but YAML lacks a viewer_construction "
            "block — add it to retrieval_gold.yaml or run with --viewers full"
        )

    # RAGAS is hybrid-only and full_access / partial_access only. Warn (don't
    # error) when the operator's --mode / --viewers selection includes cells
    # RAGAS will silently skip, so an empty `ragas` section is never a mystery.
    if args.include_ragas:
        for skipped_mode in (m for m in modes if m != "hybrid"):
            log.warning(
                "RAGAS scoring skipped for mode=%s (hybrid-only)", skipped_mode
            )
        for skipped_viewer in (
            v
            for v in viewers
            if not any(
                ragas_cell_enabled("hybrid", v, f) for f in FILTER_ORDER
            )
        ):
            log.warning(
                "RAGAS scoring skipped for viewer=%s "
                "(RAGAS runs on full_access / partial_access only)",
                skipped_viewer,
            )

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

    # US-042: post-US-037 the match_chunks predicate requires auth.uid() to
    # match either the chunk owner or an ACL row, so service-role-only
    # requests now return zero. Mint a JWT for the corpus user so the
    # owner-side retrieval (which feeds full_access pre_filter and every
    # post_filter ranking) actually finds the corpus chunks. Service-role
    # is still used for chunk_acl management via the asyncpg connection.
    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET") or LOCAL_JWT_SECRET
    anon_key = os.environ.get("SUPABASE_ANON_KEY") or service_role_key
    owner_jwt = mint_user_jwt(CORPUS_USER_ID, CORPUS_USER_EMAIL, jwt_secret)
    owner_headers = user_headers(owner_jwt, anon_key)

    viewer_headers: dict[ViewerKind, dict[str, str]] = {
        "full_access": owner_headers,
    }
    if "partial_access" in viewers or "no_access" in viewers:
        await ensure_viewer_users(database_url)
        if "partial_access" in viewers:
            viewer_headers["partial_access"] = user_headers(
                mint_user_jwt(PARTIAL_VIEWER_ID, PARTIAL_VIEWER_EMAIL, jwt_secret),
                anon_key,
            )
        if "no_access" in viewers:
            viewer_headers["no_access"] = user_headers(
                mint_user_jwt(NO_ACCESS_VIEWER_ID, NO_ACCESS_VIEWER_EMAIL, jwt_secret),
                anon_key,
            )

    openai_client = AsyncOpenAI(api_key=openai_api_key)
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    started = time.perf_counter()

    needs_db = any(v != "full_access" for v in viewers)
    db_conn = await asyncpg.connect(database_url) if needs_db else None
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            per_question, ragas_rows = await run_eval(
                questions,
                modes,
                viewers,
                viewer_construction,
                viewer_headers,
                stable_id_map,
                openai_client,
                http,
                supabase_url,
                owner_headers,
                db_conn,
                generation_gold=generation_gold,
                anthropic_client=anthropic_client,
                include_ragas=args.include_ragas,
            )
    finally:
        if db_conn is not None:
            await db_conn.close()

    aggregates = aggregate(per_question, modes)
    elapsed_s = round(time.perf_counter() - started, 2)

    # RAGAS scoring: a single batched call over the rows collected from the
    # gated cells. score_with_ragas raises a clear RuntimeError if the `ragas`
    # package is missing. build_ragas_section assembles the per-question rows
    # plus the by-cell aggregates (US-003). Three gate families then run over
    # those aggregates: check_operational_gates (US-005) is fixed-threshold and
    # red; check_diagnostic_gates (US-006) compares against the rolling weekly
    # history and is yellow; check_score_regressions (US-007) compares the
    # RAGAS scores against their rolling median, escalating to red only when
    # the cross-family Claude judge corroborates the same-cell drop. Any red
    # finding fails the run (exit 1); yellow findings never do. All findings
    # ride along under `ragas.gate_findings` so the weekly workflow can read
    # them.
    ragas_section: dict[str, Any] | None = None
    ragas_gate_findings: list[GateFinding] = []
    diagnostic_findings: list[GateFinding] = []
    if args.include_ragas:
        ragas_results = await score_with_ragas(ragas_rows, RAGAS_JUDGE_MODEL)
        ragas_section = build_ragas_section(ragas_results, RAGAS_JUDGE_MODEL)
        operational_findings = check_operational_gates(ragas_section["aggregates"])
        history = [
            snap.get("ragas", {}).get("aggregates", {})
            for snap in load_ragas_history()
        ]
        diagnostic_findings = check_diagnostic_gates(
            ragas_section["aggregates"], history
        )
        custom_judge_history = [
            snap.get("aggregates", {}) for snap in load_custom_judge_history()
        ]
        regression_findings = check_score_regressions(
            {"ragas": ragas_section, "aggregates": aggregates},
            history,
            custom_judge_history,
        )
        ragas_gate_findings = (
            operational_findings + diagnostic_findings + regression_findings
        )
        ragas_section["gate_findings"] = [asdict(f) for f in ragas_gate_findings]

    # US-009: E6 second-workspace zero-leak eval. Additive — runs only under
    # --include-e6, strictly AFTER the E4 sweep above (per_question / aggregates
    # are already computed), and seeds Workspace B with stable_id-less chunks so
    # fetch_stable_id_map and the E4 sweep never observe them (E4 stays
    # bit-for-bit). A cross-workspace leak — or a structurally blind positive
    # control — fails the run below, exactly like a red gate finding.
    e6_result: E6Result | None = None
    if args.include_e6:
        e6_viewer_headers = user_headers(
            mint_user_jwt(E6_VIEWER_ID, E6_VIEWER_EMAIL, jwt_secret), anon_key
        )
        async with httpx.AsyncClient(timeout=30.0) as e6_http:
            e6_result = await run_e6(
                questions=questions,
                modes=modes,
                stable_id_map=stable_id_map,
                run_query=run_query,
                openai_client=openai_client,
                http=e6_http,
                supabase_url=supabase_url,
                e6_viewer_headers=e6_viewer_headers,
                database_url=database_url,
            )

    results = {
        "generated_at": started_at,
        "elapsed_s": elapsed_s,
        "modes": list(modes),
        "viewers": list(viewers),
        "n_questions": len(per_question),
        "n_corpus_chunks": len(stable_id_map),
        "generation_included": bool(args.include_generation),
        "generation_model": GENERATION_MODEL if args.include_generation else None,
        "judge_model": JUDGE_MODEL if args.include_generation else None,
        "per_question": per_question,
        "aggregates": aggregates,
    }
    if ragas_section is not None:
        results["ragas"] = ragas_section
    if e6_result is not None:
        results["e6"] = e6_result.to_dict()

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

    args.summary.write_text(
        render_summary(
            aggregates, modes, ragas_section, diagnostic_findings, e6_result
        ),
        encoding="utf-8",
    )

    suffix = " + generation" if args.include_generation else ""
    print(
        f"retrieval eval done{suffix}: {len(per_question)} questions × "
        f"{len(modes)} modes × {len(viewers)} viewers in {elapsed_s}s → {out_path}"
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

    if e6_result is not None:
        print(
            f"  E6 (workspace zero-leak): "
            f"{'PASS' if e6_result.passed else 'FAIL'} — "
            f"{len(e6_result.leaking_rows)} leaking row(s), "
            f"positive control "
            f"{'ok' if e6_result.positive_control_ok else 'BLIND'}"
        )

    # US-005: a red operational gate finding fails the run. The JSON +
    # summary.md are already written above, so the weekly workflow can still
    # read `ragas.gate_findings` and file issues despite the non-zero exit.
    red_findings = [f for f in ragas_gate_findings if f.severity == "red"]
    if red_findings:
        log.error(
            "%d red operational gate finding(s) — failing the run:",
            len(red_findings),
        )
        for f in red_findings:
            log.error("  [%s] %s", f.tag, f.message)
        return 1

    # US-009: E6 is a pinned security invariant (CONTEXT E8 "Security/correctness
    # gate"). A cross-workspace leak, or a positive control that proves the eval
    # is structurally blind, is a hard fail — non-downgradable, no comment/off
    # setting. The JSON + summary.md are already written, so the leak detail is
    # preserved despite the non-zero exit.
    if e6_result is not None and not e6_result.passed:
        if e6_result.leak_detected:
            log.error(
                "E6 CROSS-WORKSPACE LEAK — a non-member of Workspace B retrieved "
                "B's gold on %d row(s):",
                len(e6_result.leaking_rows),
            )
            for row in e6_result.leaking_rows[:20]:
                log.error(
                    "  %s %s/%s recall@10=%.3f",
                    row["question_id"], row["mode"], row["filter"],
                    row["recall_at_10"],
                )
        else:
            log.error(
                "E6 positive control retrieved NOTHING — the eval is structurally "
                "blind, so its zero-leak result is a false pass. Failing the run."
            )
        return 1
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
