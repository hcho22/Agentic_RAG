"""US-043: scale-benchmark runner — recall@5 vs ef_search × selectivity.

Demonstrates the HNSW recall-collapse phenomenon under selective permission
filters that the writeup (US-044) names. For each (question × viewer ×
ef_search) triple, calls match_chunks under the viewer's own JWT (so the
SQL permission predicate runs against the viewer's chunk_acl rows
written by `db_seed.wikipedia_seed`) and computes recall@5.

"Gold" for the recall metric is the top-5 chunks returned at the highest
ef_search value (`ef_search_for_gold`, default 500). This is a near-exact
NN reference for a viewer's visible-chunks set; lower ef_search values
are then measured by overlap with that reference. By construction, the
ef_search=`ef_search_for_gold` cell is always 1.0; the interesting story
is what happens at ef_search ∈ {40, 80, 200}.

Output:
    evals/permissions_scale/results/<ISO-timestamp>.json   — per-question detail
    evals/permissions_scale/summary.md                     — one table:
                                                              rows = selectivity,
                                                              columns = ef_search,
                                                              cells = mean recall@5

Run:
    python -m evals.permissions_scale.runner
    python -m evals.permissions_scale.runner --out /tmp/scale.json

Reads env:
    SUPABASE_URL                       — local: http://127.0.0.1:54321
    SUPABASE_SERVICE_ROLE_KEY          — for the chunk_id→stable_id lookup
    OPENAI_API_KEY                     — for embedding the queries
    CORPUS_SEED_DATABASE_URL | DATABASE_URL  — for the chunk_id lookups
    SUPABASE_JWT_SECRET                — falls back to local-dev default
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import jwt as pyjwt
import yaml
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from embeddings import embed_texts, to_pgvector  # noqa: E402

log = logging.getLogger("agentic_rag.evals.permissions_scale")

DEFAULT_CONFIG = Path(__file__).resolve().parent / "scale_gold.yaml"
DEFAULT_QUESTIONS = ROOT / "evals" / "retrieval" / "retrieval_gold.yaml"
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_SUMMARY = Path(__file__).resolve().parent / "summary.md"

# Local-supabase default JWT secret. CI / hosted overrides via env.
LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"

# Mirrors backend/retrieval.py constants. Duplicated here so the runner
# stays decoupled — this benchmark calls match_chunks directly to pass
# `ef_search`, which the production search_documents() doesn't expose.
#
# Threshold 0.0 (not the production 0.3): the scale benchmark measures
# HNSW *graph-walk* behaviour under selective ACL filters, not retrieval
# quality. The questions are Acme-domain (refund policy, loyalty tier);
# the corpus is Wikipedia. No Acme query will hit cosine similarity 0.3
# against any Wikipedia chunk, so the production threshold returns 0 rows
# and recall collapses to 0 across the board (degenerate). Setting the
# threshold to 0 forces match_chunks to return its top-k by distance
# regardless of magnitude — exactly what we need to compare the rankings
# at different ef_search values.
SCALE_BENCHMARK_THRESHOLD = 0.0


def load_config(path: Path) -> dict[str, Any]:
    with path.open() as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"{path} did not parse to a dict")
    for key in ("viewers", "question_ids", "ef_search_values", "ef_search_for_gold", "top_k"):
        if key not in cfg:
            raise RuntimeError(f"{path} missing required key: {key}")
    if cfg["ef_search_for_gold"] not in cfg["ef_search_values"]:
        raise RuntimeError(
            f"ef_search_for_gold ({cfg['ef_search_for_gold']}) must appear in "
            f"ef_search_values ({cfg['ef_search_values']})"
        )
    return cfg


def load_questions(path: Path, ids: list[str]) -> list[dict[str, Any]]:
    """Pull the requested question IDs from retrieval_gold.yaml."""
    with path.open() as f:
        data = yaml.safe_load(f)
    by_id = {q["id"]: q for q in data["questions"]}
    missing = [qid for qid in ids if qid not in by_id]
    if missing:
        raise RuntimeError(f"question IDs not found in {path}: {missing}")
    return [by_id[qid] for qid in ids]


def mint_user_jwt(user_id: uuid.UUID, email: str, secret: str) -> str:
    """HS256 JWT shaped like a Supabase auth token. Long expiry (1d)."""
    now = int(time.time())
    payload = {
        "iss": "agentic-rag-permissions-scale",
        "sub": str(user_id),
        "email": email,
        "role": "authenticated",
        "aud": "authenticated",
        "iat": now,
        "exp": now + 86400,
    }
    return pyjwt.encode(payload, secret, algorithm="HS256")


def user_headers(jwt_token: str, anon_or_service_key: str) -> dict[str, str]:
    return {
        "apikey": anon_or_service_key,
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }


async def fetch_wikipedia_stable_id_map(database_url: str) -> dict[str, str]:
    """Return `{chunk.id: stable_id}` for the wikipedia corpus only.

    Filtered by stable_id prefix so the retrieval-correctness Acme corpus
    chunks (`<filename-slug>:<index>`) don't leak into the scale eval's
    candidate set.
    """
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            """
            select id, stable_id
              from public.chunks
             where stable_id like 'wikipedia-%'
            """
        )
    finally:
        await conn.close()
    return {str(r["id"]): r["stable_id"] for r in rows}


async def call_match_chunks(
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    headers: dict[str, str],
    query_embedding_literal: str,
    match_count: int,
    ef_search: int,
) -> list[dict[str, Any]]:
    """Direct match_chunks RPC call — bypasses search_documents() so we can
    pass `ef_search`, which the production wrapper doesn't expose."""
    payload = {
        "query_embedding": query_embedding_literal,
        "match_threshold": SCALE_BENCHMARK_THRESHOLD,
        "match_count": match_count,
        "filter_topics": None,
        "filter_document_type": None,
        "filter_date_from": None,
        "filter_date_to": None,
        "ef_search": ef_search,
    }
    r = await http.post(
        f"{supabase_url}/rest/v1/rpc/match_chunks",
        headers=headers,
        json=payload,
    )
    r.raise_for_status()
    return r.json()


def recall_at_k(gold_ids: set[str], retrieved_ids: list[str], k: int) -> float:
    if not gold_ids:
        return 0.0
    top_k = set(retrieved_ids[:k])
    return len(gold_ids & top_k) / len(gold_ids)


async def run_eval(
    questions: list[dict[str, Any]],
    cfg: dict[str, Any],
    viewer_headers: dict[str, dict[str, str]],
    stable_id_map: dict[str, str],
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
) -> list[dict[str, Any]]:
    """Per (question × viewer × ef_search): call match_chunks, record top-k.

    Embeds each question once and re-uses the embedding across all
    (viewer, ef_search) combos for that question — same query vector, so
    re-embedding would just add cost and embedding-API jitter.
    """
    top_k = int(cfg["top_k"])
    ef_search_values = list(cfg["ef_search_values"])
    ef_gold = int(cfg["ef_search_for_gold"])

    per_question: list[dict[str, Any]] = []
    for q in questions:
        qid = q["id"]
        question_text = q["question"]
        embeddings = await embed_texts(openai_client, [question_text])
        if not embeddings:
            raise RuntimeError(f"empty embedding for question {qid}")
        query_literal = to_pgvector(embeddings[0])

        per_viewer: dict[str, dict[str, Any]] = {}
        for viewer in cfg["viewers"]:
            vname = viewer["name"]
            headers = viewer_headers[vname]
            per_ef: dict[str, dict[str, Any]] = {}
            for ef in ef_search_values:
                rows = await call_match_chunks(
                    openai_client,
                    http,
                    supabase_url,
                    headers,
                    query_literal,
                    match_count=top_k,
                    ef_search=ef,
                )
                # Map back to stable_ids; chunks not in our wikipedia set
                # (shouldn't happen, but be defensive) are dropped.
                top_stable_ids = [
                    stable_id_map[r["id"]] for r in rows if r["id"] in stable_id_map
                ]
                per_ef[str(ef)] = {
                    "top_stable_ids": top_stable_ids,
                    "n_returned": len(rows),
                }
            # Compute recall@5 vs the ef_search_for_gold cell. Doing it
            # here (post-loop, in-Python) keeps the RPC count to exactly
            # |viewers| × |ef_values| per question — no extra ground-truth
            # call.
            gold_ids = set(per_ef[str(ef_gold)]["top_stable_ids"])
            for ef in ef_search_values:
                per_ef[str(ef)]["recall_at_5"] = recall_at_k(
                    gold_ids, per_ef[str(ef)]["top_stable_ids"], top_k
                )
            per_viewer[vname] = per_ef
        per_question.append({
            "id": qid,
            "question": question_text,
            "by_viewer": per_viewer,
        })
    return per_question


def aggregate(
    per_question: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Mean recall@5 per (viewer × ef_search), shaped for the summary table."""
    ef_values = [str(e) for e in cfg["ef_search_values"]]
    by_viewer_ef: dict[str, dict[str, float]] = {}
    for viewer in cfg["viewers"]:
        vname = viewer["name"]
        by_viewer_ef[vname] = {}
        for ef in ef_values:
            total = 0.0
            n = 0
            for q in per_question:
                cell = q["by_viewer"].get(vname, {}).get(ef)
                if cell is None:
                    continue
                total += float(cell["recall_at_5"])
                n += 1
            if n > 0:
                by_viewer_ef[vname][ef] = round(total / n, 4)
    return {"recall_at_5_by_viewer_ef": by_viewer_ef}


def render_summary(
    aggregates: dict[str, Any],
    cfg: dict[str, Any],
    n_questions: int,
    elapsed_s: float,
) -> str:
    """Single-table markdown wrapped in EVAL_SUMMARY markers (US-044 embeds)."""
    ef_values = [str(e) for e in cfg["ef_search_values"]]
    ef_gold = str(cfg["ef_search_for_gold"])
    by_viewer = aggregates["recall_at_5_by_viewer_ef"]

    header = ["| Viewer | Visible chunks | Selectivity |"]
    sep = ["|---|---|---|"]
    for ef in ef_values:
        suffix = " (gold)" if ef == ef_gold else ""
        header.append(f" ef_search={ef}{suffix} |")
        sep.append("---|")
    header_line = "".join(header)
    sep_line = "".join(sep)

    lines: list[str] = [
        "<!-- BEGIN EVAL_SUMMARY -->",
        "",
        "### Permissions scale: recall@5 vs ef_search × selectivity",
        "",
        f"_Wikipedia corpus, {cfg['corpus']['total_chunks']:,} chunks; "
        f"mean across {n_questions} multi-hop queries; {elapsed_s}s wall._",
        "",
        f"_Gold = top-5 returned at ef_search={ef_gold} (the most exhaustive "
        f"sweep); lower ef_search values are scored by overlap with that set._",
        "",
        header_line,
        sep_line,
    ]
    total_chunks = int(cfg["corpus"]["total_chunks"])
    for viewer in cfg["viewers"]:
        vname = viewer["name"]
        visible = int(viewer["visible_chunks"])
        sel = visible / total_chunks * 100
        row = [f"| {vname} | {visible:,} | {sel:.1f}% |"]
        for ef in ef_values:
            v = by_viewer.get(vname, {}).get(ef)
            row.append(f" {v:.3f} |" if v is not None else " — |")
        lines.append("".join(row))

    lines += ["", "<!-- END EVAL_SUMMARY -->", ""]
    return "\n".join(lines)


def check_recall_floor(
    aggregates: dict[str, Any],
    cfg: dict[str, Any],
) -> tuple[bool, str]:
    """Apply the configured recall floor. Returns (ok, message).

    The floor is the nightly workflow's "regression alarm": if recall@5
    at the configured (viewer × ef_search) cell drops below
    `min_recall_at_5`, the workflow exits non-zero. The default floor
    (0.10) is intentionally loose — see scale_gold.yaml comment.
    """
    floor = cfg.get("recall_floor")
    if not floor:
        return True, "no recall_floor configured"
    vname = floor["viewer_name"]
    ef = str(floor["ef_search"])
    threshold = float(floor["min_recall_at_5"])
    actual = aggregates["recall_at_5_by_viewer_ef"].get(vname, {}).get(ef)
    if actual is None:
        return False, f"recall_floor cell missing: viewer={vname} ef_search={ef}"
    ok = actual >= threshold
    sign = ">=" if ok else "<"
    return ok, (
        f"recall@5 floor: viewer={vname} ef_search={ef} actual={actual:.3f} "
        f"{sign} threshold={threshold:.3f}"
    )


async def amain() -> int:
    parser = argparse.ArgumentParser(description="US-043 permissions-scale eval")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="JSON output path; default: results/<ISO-timestamp>.json",
    )
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument(
        "--enforce-floor", action="store_true",
        help="Exit non-zero if recall_floor (configured in YAML) is breached. "
             "The nightly workflow sets this; local runs default to off.",
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
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is required")
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required (queries are embedded)")
    database_url = (
        os.environ.get("CORPUS_SEED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not database_url:
        raise RuntimeError("CORPUS_SEED_DATABASE_URL or DATABASE_URL is required")

    cfg = load_config(args.config)
    questions = load_questions(args.questions, list(cfg["question_ids"]))

    stable_id_map = await fetch_wikipedia_stable_id_map(database_url)
    if not stable_id_map:
        raise RuntimeError(
            "no wikipedia chunks found — run `python -m db_seed.wikipedia_seed` first"
        )
    log.info("permissions_scale: %d wikipedia chunks loaded", len(stable_id_map))

    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET") or LOCAL_JWT_SECRET
    anon_key = os.environ.get("SUPABASE_ANON_KEY") or service_role_key
    viewer_headers: dict[str, dict[str, str]] = {}
    for viewer in cfg["viewers"]:
        token = mint_user_jwt(uuid.UUID(viewer["id"]), viewer["email"], jwt_secret)
        viewer_headers[viewer["name"]] = user_headers(token, anon_key)

    openai_client = AsyncOpenAI(api_key=openai_api_key)
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=60.0) as http:
        per_question = await run_eval(
            questions, cfg, viewer_headers, stable_id_map,
            openai_client, http, supabase_url,
        )

    aggregates = aggregate(per_question, cfg)
    elapsed_s = round(time.perf_counter() - started, 2)

    results = {
        "generated_at": started_at,
        "elapsed_s": elapsed_s,
        "n_questions": len(per_question),
        "n_corpus_chunks": len(stable_id_map),
        "config": {
            "ef_search_values": list(cfg["ef_search_values"]),
            "ef_search_for_gold": cfg["ef_search_for_gold"],
            "top_k": cfg["top_k"],
            "viewers": [
                {
                    "name": v["name"],
                    "id": v["id"],
                    "visible_chunks": v["visible_chunks"],
                }
                for v in cfg["viewers"]
            ],
        },
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

    args.summary.write_text(
        render_summary(aggregates, cfg, len(per_question), elapsed_s),
        encoding="utf-8",
    )

    print(
        f"permissions_scale eval done: {len(per_question)} questions × "
        f"{len(cfg['viewers'])} viewers × {len(cfg['ef_search_values'])} "
        f"ef_search values in {elapsed_s}s → {out_path}"
    )
    for viewer in cfg["viewers"]:
        vname = viewer["name"]
        cells = aggregates["recall_at_5_by_viewer_ef"].get(vname, {})
        cell_str = " ".join(
            f"ef={ef}:{cells[str(ef)]:.3f}" for ef in cfg["ef_search_values"]
            if str(ef) in cells
        )
        print(f"  {vname}: {cell_str}")

    ok, message = check_recall_floor(aggregates, cfg)
    print(f"  {message}")
    if args.enforce_floor and not ok:
        log.error("recall floor breached — exiting 1")
        return 1
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
