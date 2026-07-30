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

# Cap on how much of an arbitrary exception's text is interpolated into the
# PUBLISHED failure notice. `summary.md` is copied into
# `docs/permissions-scale-nightly/<DATE>.md` and committed to a public repo, and
# GitHub Actions secret masking applies to log OUTPUT, not to files a step
# writes and commits - so this artifact is a weaker boundary than a log line
# (AGENTS.md invariant 3: secrets are server-side only, never a response body or
# log line). Realistic messages here are benign (httpx includes the URL but not
# headers; the OpenAI SDK redacts the key), but the text is arbitrary and
# unbounded, and an unbounded copy of an unknown string is not a boundary at all.
# Bounding the length is the honest mitigation: it is predictable and it does not
# claim a sanitisation we do not perform. We deliberately do NOT pattern-match or
# scrub, which would advertise a guarantee the regexes could not keep. The full
# untruncated text still goes to the run log, which IS masked.
MAX_PUBLISHED_EXCEPTION_CHARS = 400


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


class DegenerateRunError(RuntimeError):
    """The run produced no signal, so there is nothing to score.

    A recall metric can only distinguish "retrieved the wrong things" from
    "retrieved nothing" if the no-signal case is refused rather than scored.
    Gold here is not a fixture - it is the `ef_search_for_gold` cell of this
    same sweep - so a viewer that can see nothing yields an empty gold set,
    and every cell then scores a well-formed, entirely meaningless 0.000 that
    is indistinguishable from a real measured regression. That is what the
    nightly published every night from 2026-06-19 onward while the wikipedia
    seed was leaving the viewers with no workspace_membership row.

    Raising instead is what makes the failure diagnosable from the nightly log
    alone, so every message names the viewer, the question id, and the
    ef_search value that went dark.

    INVARIANT: a `DegenerateRunError` message must be authored here, from
    config-derived values only - it must NEVER wrap an upstream exception's
    text. `render_degenerate_summary` publishes this message VERBATIM and
    unbounded (deliberately: the actionable "reseed with `python -m
    db_seed.wikipedia_seed`" instruction sits past the 400-char cap that bounds
    the arbitrary-exception notice), and that published file is committed to a
    public repo where Actions secret masking does not reach - see
    `MAX_PUBLISHED_EXCEPTION_CHARS` and AGENTS.md invariant 3. Self-authored
    text is what makes publishing it verbatim safe, so a future change that
    starts raising `DegenerateRunError(f"...{some_upstream_exc}")` is a
    violation of this boundary, not an innocuous refactor: route arbitrary
    exception text through the bounded `render_failed_summary` path instead.
    """


def _assert_cell_has_signal(
    viewer_name: str,
    qid: str,
    ef: int,
    n_returned: int,
    mapped_stable_ids: list[str],
    *,
    is_gold_cell: bool,
) -> None:
    """Refuse a retrieval cell that came back with nothing to score.

    Called the moment each match_chunks response lands - the earliest point the
    degeneracy is knowable, and before gold exists at all - so a caller that
    never reaches `recall_at_k` cannot route around the guard.

    ONE rule, uniformly applied: the GOLD cell (`ef_search_for_gold`) must yield
    a non-empty MAPPED set; a non-gold cell may yield anything at all, including
    nothing. Gold is built from that one cell, so an empty mapped set there means
    gold is empty and the entire sweep is unscoreable. In a non-gold cell the
    same shape carries real signal: the viewer retrieved rows whose top-k simply
    contains none of gold, and recall@5 = 0.0 is the arithmetically correct,
    legitimately measured answer - a low-ef graph walk finding no gold candidate
    inside a sparse ACL set is the very phenomenon this eval exists to observe,
    and must be reported rather than abort the sweep. Scoping both refusals the
    same way is what keeps the rule statable in one sentence; the earlier split
    (zero rows tolerated in a non-gold cell, but unmappable rows fatal there)
    aborted on strictly MORE signal than it let through.

    This still closes the incident class the guard was written for: a
    permissions/membership bug blinds EVERY cell including gold, so gold refuses
    and the run fails loudly. Note this is forward-compatibility only today -
    match_chunks' `distinct on (c.id) ... order by c.id` subquery prevents HNSW
    ordering, so the scan is exact and `ef_search` is currently inert, making
    the two behaviours identical on the present schema.

    The two gold-cell causes stay distinct in the message because they call for
    different fixes: no rows at all means the viewer is blind (missing chunk_acl
    grants, or missing workspace_membership), whereas rows that all fall outside
    the wikipedia corpus mean a stale/foreign stable_id map, so the top-k we
    would score is not the top-k the viewer got.
    """
    where = f"viewer={viewer_name} question={qid} ef_search={ef}"
    if not is_gold_cell:
        return
    if n_returned == 0:
        raise DegenerateRunError(
            f"match_chunks returned 0 rows for the gold cell {where} - the "
            f"viewer can see nothing, so there is no signal to score. Check that "
            f"the viewer has chunk_acl grants AND a public.workspace_membership "
            f"row for the wikipedia documents' workspace (the US-003 membership "
            f"clause is AND-ed with the owner-OR-ACL predicate, so a missing "
            f"membership row hides even explicitly granted chunks); reseed with "
            f"`python -m db_seed.wikipedia_seed`."
        )
    if not mapped_stable_ids:
        raise DegenerateRunError(
            f"match_chunks returned {n_returned} rows for the gold cell {where} "
            f"but none mapped to a wikipedia stable_id - the chunk_id→stable_id "
            f"map is stale or the viewer is retrieving a foreign corpus, so gold "
            f"is empty. Nothing to score; reseed with "
            f"`python -m db_seed.wikipedia_seed`."
        )


def recall_at_k(
    gold_ids: set[str],
    retrieved_ids: list[str],
    k: int,
    *,
    context: str = "",
) -> float:
    """recall@k against `gold_ids`. Raises rather than scoring an empty gold set.

    The empty-gold branch used to `return 0.0`, which is the bug this guard
    exists to close: it turned "we measured nothing" into a reportable number.
    """
    if not gold_ids:
        where = f" for {context}" if context else ""
        raise DegenerateRunError(
            f"empty gold set{where} - gold is the ef_search_for_gold cell of this "
            f"same sweep, so an empty gold set means that cell returned nothing "
            f"scoreable. Refusing to report 0.000 for a run that measured nothing."
        )
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
                # Refuse the cell here - the earliest point the degeneracy is
                # knowable, and before gold exists - so no caller can reach the
                # scoring chain with a no-signal cell.
                #
                # DECIDED: this raises from inside the per-(question × viewer ×
                # ef) loop, so the FIRST blind gold cell aborts the WHOLE sweep,
                # including viewers that would have measured fine. That is the
                # intended tradeoff, not an oversight:
                #   (i)  it is the fail-closed side of AGENTS.md invariant 4, and
                #        metric integrity is exactly the class where a miss
                #        misleads - a table carrying two healthy rows next to one
                #        refusal invites being read as "mostly fine" while a
                #        tenant-visibility regression is live;
                #   (ii) triage is not blocked by the abort, because the refusal
                #        message already names the viewer, question and ef_search;
                #   (iii) the recall_floor check is lost with the sweep, but that
                #        check is a regression alarm on a MEASURED number, and
                #        once any gold cell is blind there is no measured number
                #        to alarm on - a floor evaluated over a partly-refused
                #        sweep would be a weaker signal, not a stronger one.
                # If preserving the other viewers' numbers ever becomes worth it,
                # the shape is: collect per-viewer refusals and raise AFTER the
                # sweep instead of during it, and teach render_summary to emit an
                # explicit REFUSED row, so a partial table can never be mistaken
                # for a complete one.
                _assert_cell_has_signal(
                    vname, qid, int(ef), len(rows), top_stable_ids,
                    is_gold_cell=int(ef) == ef_gold,
                )
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
                    gold_ids,
                    per_ef[str(ef)]["top_stable_ids"],
                    top_k,
                    context=f"viewer={vname} question={qid} ef_search={ef}",
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
    """Mean recall@5 per (viewer × ef_search), shaped for the summary table.

    Raises `DegenerateRunError` on a (viewer × ef_search) pair that no question
    contributed to. The old `if n > 0` skipped such a pair, leaving the cell to
    render as a dash and the recall floor to report a vague "cell missing" - the
    same class of quiet degradation as scoring an empty gold set. There is no
    legitimate reason for a configured viewer to have zero measured questions,
    so it is a failure, not a gap in the table. Because this raises, every
    configured cell is populated by construction, which is why `render_summary`
    indexes the aggregate directly instead of carrying a placeholder branch.
    """
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
            if n == 0:
                raise DegenerateRunError(
                    f"no measured questions for viewer={vname} ef_search={ef} "
                    f"across {len(per_question)} question(s) - nothing to "
                    f"average. Refusing to render a summary table for a run "
                    f"that measured nothing."
                )
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
            # `aggregate` raises rather than skipping a cell, so every
            # configured viewer × ef_search pair is populated by construction.
            row.append(f" {by_viewer[vname][ef]:.3f} |")
        lines.append("".join(row))

    lines += ["", "<!-- END EVAL_SUMMARY -->", ""]
    return "\n".join(lines)


def _render_notice(
    heading: str,
    explanation: str,
    detail: str,
    started_at: str,
) -> str:
    """Marker-wrapped "no numbers this run" summary body.

    Keeps the same `<!-- BEGIN/END EVAL_SUMMARY -->` markers as the real table
    because `docs/_embed_eval_summaries.py` keys off exactly that pair; dropping
    them would break the embed into `docs/permissions-aware-rag.md` instead of
    propagating the failure into it.
    """
    return "\n".join([
        "<!-- BEGIN EVAL_SUMMARY -->",
        "",
        f"### {heading}",
        "",
        f"_Run started {started_at}. {explanation}_",
        "",
        "```",
        detail,
        "```",
        "",
        "<!-- END EVAL_SUMMARY -->",
        "",
    ])


def render_degenerate_summary(exc: DegenerateRunError, started_at: str) -> str:
    """The summary written IN PLACE OF the table when the run measured nothing.

    The nightly's publish step is `if: always()`, so the failure notice - not
    silence - is what the published `<DATE>.md` should carry: "no numbers" has
    to be stated, because an absent statement reads as an absent job rather than
    as a refused measurement.

    The `DegenerateRunError` message is carried VERBATIM and unbounded so the
    published nightly names the viewer, question id and ef_search that went
    dark, plus the reseed command, without anyone needing the CI log. That
    asymmetry with `render_failed_summary` (which caps the text at
    `MAX_PUBLISHED_EXCEPTION_CHARS`) is deliberate and is safe for exactly one
    reason: every `DegenerateRunError` message is authored inside this module
    from config-derived values, never wrapped around an upstream exception. See
    the invariant on `DegenerateRunError` - if that ever stops holding, this
    function becomes an unbounded copy of an unknown string into a file
    committed to a public repo.
    """
    return _render_notice(
        "Permissions scale: DEGENERATE RUN - no numbers published",
        "The harness refused to score this run: it produced no signal, so there "
        "is no recall@5 table. The numbers below are absent, not zero - see the "
        "invariant in `AGENTS.md` on refusing to score a run that produced no "
        "signal.",
        str(exc),
        started_at,
    )


def render_failed_summary(exc: BaseException, started_at: str) -> str:
    """The same notice, for a run that died of something other than degeneracy.

    A `DegenerateRunError` is not the only way a run ends with no numbers: an
    unseeded database, a viewer JWT the API rejects, or an embedder outage all
    abort the run just as thoroughly. So every failure on the eval path lands
    here. The exception type is included alongside the message because, unlike a
    `DegenerateRunError`, an arbitrary exception's text is not guaranteed to say
    what kind of failure it was.

    The type is carried in full - it is always safe and it is the single most
    useful triage signal - but the MESSAGE is truncated to
    `MAX_PUBLISHED_EXCEPTION_CHARS`, because unlike the run log this notice gets
    committed to a public repository where Actions secret masking does not
    reach. See that constant for the full rationale.

    The "go read the job log" pointer is attached only when something was
    actually dropped. Realistic httpx/asyncpg messages are well under the cap,
    and sending a triager off to dig out the full text of a message that is
    already complete in front of them is a false lead.
    """
    message = str(exc)
    truncated = len(message) > MAX_PUBLISHED_EXCEPTION_CHARS
    if truncated:
        message = (
            message[:MAX_PUBLISHED_EXCEPTION_CHARS]
            + f" [... truncated at {MAX_PUBLISHED_EXCEPTION_CHARS} characters]"
        )
    explanation = (
        "The run did not complete, so there is no recall@5 table. Any numbers "
        "you were expecting here are absent, not measured."
    )
    if truncated:
        explanation += (
            " The message below is truncated for publication; the full text and "
            "traceback are in the nightly job log."
        )
    return _render_notice(
        "Permissions scale: RUN FAILED - no numbers published",
        explanation,
        f"{type(exc).__name__}: {message}",
        started_at,
    )


def write_summary(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_failure_notice(path: Path, text: str) -> None:
    """Write a "no numbers published" notice - unless the target is the tracked default.

    The two branches differ on purpose; do not collapse them.

    The rule keys off the resolved TARGET, not off whether `--summary` was passed
    explicitly, because it is the target that decides whether writing can dirty a
    tracked file. An explicitness-based rule would let a caller who passes
    `--summary evals/permissions_scale/summary.md` reintroduce exactly the footgun
    described below, so do not "fix" this into a sentinel default that can tell
    the two apart.

    * A target OTHER than the in-repo default means the notice is wanted as an
      artifact. The nightly is that caller: it points the runner at a scratch
      path and its publish step copies whatever is there into
      `docs/permissions-scale-nightly/<DATE>.md`, so the notice is how a refused
      or crashed run states "no numbers" instead of publishing silence. Write it.

    * The DEFAULT path is `evals/permissions_scale/summary.md`, which is
      git-tracked and holds the last good table. Nothing is keyed to it on a
      failure, so writing there has exactly one effect: it leaves a modified
      tracked file behind for a developer to commit by accident. That is a live
      footgun - running this runner before `python -m db_seed.wikipedia_seed`
      raises `DegenerateRunError`, and a later `git commit -a` would replace the
      committed baseline table with a DEGENERATE RUN notice and propagate it into
      `docs/permissions-aware-rag.md` via `docs/_embed_eval_summaries.py`. Skip it.

    This used to write unconditionally, to stop the nightly republishing the
    checked-out table when a run aborted without rewriting it. That reason is
    gone: the workflow now deletes the checked-out summary after checkout AND
    publishes from its own `--summary` path, so the tracked file no longer needs
    defending by the runner. The notice is an artifact concern only - the caller
    keeps its non-zero exit either way, and the reason is logged at ERROR either
    way, so skipping the write never softens a failure.
    """
    if path.resolve() == DEFAULT_SUMMARY:
        log.error(
            "permissions_scale: leaving the git-tracked %s untouched; pass "
            "--summary <path> to capture the failure notice as an artifact",
            DEFAULT_SUMMARY.name,
        )
        return
    write_summary(path, text)


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
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    # Preflight validation runs OUTSIDE the guarded region below, deliberately.
    # A missing env var or a bad --config path is a mistake about how the run was
    # INVOKED; nothing has been measured yet, so there is no stale-table hazard
    # to close and no reason to touch `summary.md` - which is git-tracked and
    # holds the last good table. Guarding these would mean a developer who simply
    # forgot to export SUPABASE_URL silently dirties a tracked file and can commit
    # the failure notice. The CI concern that might tempt someone to move this
    # back inside (a job that dies before producing numbers still publishing the
    # checked-out table) is covered upstream instead: the nightly deletes
    # summary.md immediately after checkout.
    env = _preflight_env()
    cfg = load_config(args.config)
    questions = load_questions(args.questions, list(cfg["question_ids"]))

    # The invariant this guard exists to hold: a run that produced no numbers
    # must SAY so wherever it publishes, rather than let a reader infer numbers
    # from a file that some earlier, healthier run wrote. The nightly copies its
    # summary unconditionally (`if: always()`), so silence there would read as
    # "the job didn't run" rather than "the run was refused". Hence the whole
    # MEASUREMENT path is guarded, not just the degenerate case: a refusal gets
    # the friendlier notice and exit 1, and every other failure gets the
    # catch-all notice and its traceback re-raised so the run log still shows
    # what broke. No results JSON is written on either path, because the JSON
    # write happens only after `_measure` returns. `_write_failure_notice` is
    # what decides where a notice may land - see it for why the in-repo default
    # is deliberately left untouched on these paths.
    #
    # The guard ends exactly where measurement does. Everything after it - the
    # results JSON, the real summary, the prints, the recall floor - runs on a
    # genuine, fully-measured table, and a fault there (e.g. a malformed
    # `recall_floor` block in scale_gold.yaml raising KeyError/ValueError) must
    # NOT overwrite that table with a "RUN FAILED" notice claiming the run
    # measured nothing when it measured fine.
    try:
        per_question, aggregates, elapsed_s, n_corpus_chunks = await _measure(
            cfg, questions, env,
        )
    except DegenerateRunError as exc:
        log.error("permissions_scale: degenerate run, refusing to score - %s", exc)
        _write_failure_notice(args.summary, render_degenerate_summary(exc, started_at))
        print(f"permissions_scale eval REFUSED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - re-raised below
        log.error("permissions_scale: run failed before producing numbers - %s", exc)
        _write_failure_notice(args.summary, render_failed_summary(exc, started_at))
        raise

    return _publish(
        args, cfg, per_question, aggregates, elapsed_s, n_corpus_chunks, started_at,
    )


def _preflight_env() -> dict[str, str]:
    """Resolve and validate the env the eval needs. Raises before anything runs."""
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
    return {
        "supabase_url": supabase_url,
        "service_role_key": service_role_key,
        "openai_api_key": openai_api_key,
        "database_url": database_url,
    }


async def _measure(
    cfg: dict[str, Any],
    questions: list[dict[str, Any]],
    env: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any], float, int]:
    """The measurement phase, and exactly the region `amain` guards.

    Ends the moment `aggregate` returns a table, so the guard covers everything
    that can leave the run with no numbers and nothing that happens once numbers
    exist. Returns (per_question, aggregates, elapsed_s, n_corpus_chunks).
    """
    stable_id_map = await fetch_wikipedia_stable_id_map(env["database_url"])
    if not stable_id_map:
        # An unseeded database is the canonical degenerate run: no corpus means
        # no cell can produce signal, so this is the same refusal as a blind
        # gold cell rather than an unrelated crash.
        raise DegenerateRunError(
            "no wikipedia chunks found - run `python -m db_seed.wikipedia_seed` "
            "first. Nothing to score, so no summary table is published."
        )
    log.info("permissions_scale: %d wikipedia chunks loaded", len(stable_id_map))

    jwt_secret = os.environ.get("SUPABASE_JWT_SECRET") or LOCAL_JWT_SECRET
    anon_key = os.environ.get("SUPABASE_ANON_KEY") or env["service_role_key"]
    viewer_headers: dict[str, dict[str, str]] = {}
    for viewer in cfg["viewers"]:
        token = mint_user_jwt(uuid.UUID(viewer["id"]), viewer["email"], jwt_secret)
        viewer_headers[viewer["name"]] = user_headers(token, anon_key)

    openai_client = AsyncOpenAI(api_key=env["openai_api_key"])
    started = time.perf_counter()

    async with httpx.AsyncClient(timeout=60.0) as http:
        per_question = await run_eval(
            questions, cfg, viewer_headers, stable_id_map,
            openai_client, http, env["supabase_url"],
        )
    aggregates = aggregate(per_question, cfg)

    elapsed_s = round(time.perf_counter() - started, 2)
    return per_question, aggregates, elapsed_s, len(stable_id_map)


def _publish(
    args: argparse.Namespace,
    cfg: dict[str, Any],
    per_question: list[dict[str, Any]],
    aggregates: dict[str, Any],
    elapsed_s: float,
    n_corpus_chunks: int,
    started_at: str,
) -> int:
    """Write the results JSON + the real summary table, then apply the floor.

    Everything here runs on a genuine, fully-measured table and is therefore
    deliberately OUTSIDE `amain`'s failure guard: a fault in this phase (most
    plausibly a malformed `recall_floor` block) must surface as a crash with a
    traceback, not silently replace real numbers with a "no numbers published"
    notice.
    """
    results = {
        "generated_at": started_at,
        "elapsed_s": elapsed_s,
        "n_questions": len(per_question),
        "n_corpus_chunks": n_corpus_chunks,
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

    write_summary(
        args.summary,
        render_summary(aggregates, cfg, len(per_question), elapsed_s),
    )

    print(
        f"permissions_scale eval done: {len(per_question)} questions × "
        f"{len(cfg['viewers'])} viewers × {len(cfg['ef_search_values'])} "
        f"ef_search values in {elapsed_s}s → {out_path}"
    )
    for viewer in cfg["viewers"]:
        vname = viewer["name"]
        cells = aggregates["recall_at_5_by_viewer_ef"][vname]
        cell_str = " ".join(
            f"ef={ef}:{cells[str(ef)]:.3f}" for ef in cfg["ef_search_values"]
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
