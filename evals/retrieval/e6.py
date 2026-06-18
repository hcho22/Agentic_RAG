"""US-009: E6 — second-workspace zero-leak correctness eval (security invariant).

E6 is **additive** to E4 (the `full/partial/no_access × pre/post` viewer sweep in
`runner.py`): it never touches the six-cell sweep, so E4 keeps passing
bit-for-bit. E6 introduces a *second* Workspace B and proves the org-level
isolation claim the same way E4 proves the document-level one — an
`assert leak == 0` binary invariant, not a thresholded metric.

What it proves
--------------
The Default Workspace (the corpus E4 already uses) plays the role of **Workspace
A**. E6 seeds **Workspace B** as a second copy of the gold-bearing corpus
(identical content + embeddings, fresh document/chunk UUIDs, `stable_id = NULL`
so the copy is invisible to `runner.fetch_stable_id_map` and E4 stays
untouched).

A single **cross-workspace viewer** is constructed so that *workspace
membership* is the **only** thing separating it from B's content:

* member of Workspace A (Default), and
* holds an ACL grant on **both** A's gold chunks **and** B's gold chunks, but
* is **not** a member of Workspace B.

Because the membership clause is AND-ed under owner-OR-ACL inside `match_chunks`
/ `keyword_search` (migrations 20260617120300 / 20260617120400), the viewer can
retrieve A's gold (member + ACL) yet must retrieve **zero** of B's gold (ACL but
not a member). The negative assertion is `recall@10 == 0.0` against B's gold for
every (mode × filter) row.

The **positive control** then adds the *same* viewer to Workspace B and confirms
B's gold *does* surface — proving the eval can detect access, so a zero is a real
zero and not a false pass from an empty corpus or a structurally blind probe.

Both filter strategies are recorded, mirroring E4:

* **pre_filter** — query AS the cross-workspace viewer; the SQL membership
  predicate enforces the boundary. This is the real leak test.
* **post_filter** — take the positive-state ranking (which *contains* B's gold)
  and drop every Workspace-B chunk in Python, modelling "boundary enforced only
  after retrieval". Structurally 0 for this construction (every B chunk is
  dropped), exactly as E4's `security_no_access` post_filter is structurally
  1.0 — kept for symmetry and to exercise the same shape.

This module owns only the E6 orchestration + its DB fixtures; the runner wires
it in behind `--include-e6` and fails the run (exit 1) when `passed` is False.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import asyncpg
import httpx
from openai import AsyncOpenAI

log = logging.getLogger("agentic_rag.evals.retrieval.e6")

# Mirrors the constants the migration + seeder pin (duplicated, not imported,
# so this module stays decoupled from db_seed exactly like runner.py does).
CORPUS_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")  # Workspace A

# Workspace B — the second tenant. Fixed UUID so the seed is idempotent across
# runs and references stay constant.
WORKSPACE_B_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d6")
WORKSPACE_B_NAME = "E6 Workspace B"

# The cross-workspace viewer. Stable UUID5 (same namespace runner.py uses for its
# eval viewers) so the auth.users + ACL rows upsert idempotently.
EVAL_VIEWER_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000000042")
E6_VIEWER_ID = uuid.uuid5(EVAL_VIEWER_NAMESPACE, "eval-viewer-cross-workspace")
E6_VIEWER_EMAIL = "eval-cross-workspace@local.test"

# Deterministic namespace for the Workspace-B copies (distinct from the corpus
# seeder's "agentic-rag/corpus/..." so A's and B's UUIDs never collide).
_B_DOC_PREFIX = "agentic-rag/corpus-wsb"

TOP_K = 10  # recall@10 — widest k, catches a leak anywhere in the top-10.

# A query callable shaped like runner.run_query (passed in to avoid a circular
# import): (mode, openai_client, http, supabase_url, headers, question) -> rows.
RunQuery = Callable[
    [str, AsyncOpenAI, httpx.AsyncClient, str, dict[str, str], str],
    Awaitable[list[Any]],
]


def b_document_uuid(slug: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{_B_DOC_PREFIX}/{slug}")


def b_chunk_uuid(stable_id: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{_B_DOC_PREFIX}/{stable_id}")


def recall_at_10(gold_ids: set[str], retrieved_ids: list[str]) -> float:
    """Per-chunk partial-credit recall@10 over chunk-UUID strings."""
    if not gold_ids:
        return 0.0
    top = set(retrieved_ids[:TOP_K])
    return len(gold_ids & top) / len(gold_ids)


# ---------------------------------------------------------------------------
# DB fixtures (idempotent; service-role asyncpg connection bypasses RLS)
# ---------------------------------------------------------------------------


async def seed_workspace_b(conn: asyncpg.Connection) -> dict[str, uuid.UUID]:
    """Copy the gold-bearing corpus into Workspace B; return `stable_id -> b_chunk_id`.

    Pure SQL copy of `content` + `embedding` from A's chunks (no OpenAI calls):
    B's chunks are byte-identical to A's, so retrieval ranks them the same. B's
    chunks carry `stable_id = NULL` so `runner.fetch_stable_id_map` (which filters
    `stable_id is not null`) never sees them — E4 stays bit-for-bit.

    Idempotent: purges any prior B copy (identified by document metadata) first;
    chunks cascade via the documents FK.
    """
    await conn.execute(
        """
        insert into public.workspaces (id, name)
        values ($1, $2)
        on conflict (id) do nothing
        """,
        WORKSPACE_B_ID,
        WORKSPACE_B_NAME,
    )

    # Purge a prior B seed so re-runs start clean (chunks cascade).
    await conn.execute(
        "delete from public.documents where (metadata->>'e6_workspace_b') = 'true'"
    )

    # A's corpus documents — only the gold-bearing corpus (corpus_seed marks them).
    a_docs = await conn.fetch(
        """
        select id, filename, byte_size, content_type, status, chunks_count,
               metadata->>'filename_slug' as slug
        from public.documents
        where (metadata->>'corpus_seed') = 'true'
          and user_id = $1
        order by filename
        """,
        CORPUS_USER_ID,
    )
    if not a_docs:
        raise RuntimeError(
            "E6: no corpus documents found — run `python -m db_seed.corpus_seed` first"
        )

    stable_to_b_chunk: dict[str, uuid.UUID] = {}
    for d in a_docs:
        slug = d["slug"]
        if not slug:
            raise RuntimeError(f"E6: corpus document {d['id']} has no filename_slug")
        b_doc_id = b_document_uuid(slug)
        await conn.execute(
            """
            insert into public.documents (
                id, user_id, workspace_id, filename, storage_path, byte_size,
                content_type, status, chunks_count, metadata
            ) values ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                jsonb_build_object('e6_workspace_b', true, 'filename_slug', $10::text))
            """,
            b_doc_id,
            CORPUS_USER_ID,
            WORKSPACE_B_ID,
            d["filename"],
            f"e6-wsb/{d['filename']}",
            d["byte_size"],
            d["content_type"],
            d["status"],
            d["chunks_count"],
            slug,
        )

        a_chunks = await conn.fetch(
            "select id, stable_id from public.chunks where document_id = $1",
            d["id"],
        )
        a_ids: list[uuid.UUID] = []
        b_ids: list[uuid.UUID] = []
        for c in a_chunks:
            sid = c["stable_id"]
            if sid is None:
                continue
            b_cid = b_chunk_uuid(sid)
            a_ids.append(c["id"])
            b_ids.append(b_cid)
            stable_to_b_chunk[sid] = b_cid
        if not a_ids:
            continue
        # Copy content + embedding from A in-place (no round-trip through Python);
        # stable_id stays NULL on the B copy. content_tsv is a STORED generated
        # column, so it repopulates automatically from the copied content.
        await conn.execute(
            """
            insert into public.chunks (
                id, document_id, user_id, chunk_index, content, embedding, stable_id
            )
            select m.b_chunk_id, $1, c.user_id, c.chunk_index, c.content, c.embedding, null
            from public.chunks c
            join unnest($2::uuid[], $3::uuid[]) as m(a_chunk_id, b_chunk_id)
              on c.id = m.a_chunk_id
            """,
            b_doc_id,
            a_ids,
            b_ids,
        )

    if not stable_to_b_chunk:
        raise RuntimeError("E6: copied 0 chunks into Workspace B")
    log.info(
        "E6: seeded Workspace B with %d documents / %d chunks",
        len(a_docs),
        len(stable_to_b_chunk),
    )
    return stable_to_b_chunk


async def ensure_e6_viewer(conn: asyncpg.Connection) -> None:
    """Create the cross-workspace viewer: member of A (Default), NOT a member of B.

    Defensively removes any leftover Workspace-B membership from a crashed prior
    run so the negative pass always starts in the no-member state — otherwise a
    stale membership row would manufacture a (false) leak.
    """
    await conn.execute(
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
        E6_VIEWER_ID,
        E6_VIEWER_EMAIL,
    )
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member')
        on conflict do nothing
        """,
        DEFAULT_WORKSPACE_ID,
        E6_VIEWER_ID,
    )
    await set_viewer_in_workspace_b(conn, member=False)


async def set_viewer_acls(
    conn: asyncpg.Connection, chunk_ids: set[uuid.UUID]
) -> None:
    """Grant the viewer a user-ACL on every chunk in `chunk_ids` (A's + B's gold).

    Replaces the viewer's prior ACL set in one transaction so re-runs don't
    accumulate. The viewer thus has owner/ACL access to *both* copies of the gold
    — leaving workspace membership as the sole differentiator the test isolates.
    """
    async with conn.transaction():
        await conn.execute(
            """
            delete from public.chunk_acl
             where principal_type = 'user' and principal_id = $1
            """,
            E6_VIEWER_ID,
        )
        if chunk_ids:
            await conn.executemany(
                """
                insert into public.chunk_acl
                  (chunk_id, principal_type, principal_id, granted_by)
                values ($1, 'user', $2, $3)
                on conflict do nothing
                """,
                [(cid, E6_VIEWER_ID, CORPUS_USER_ID) for cid in chunk_ids],
            )


async def set_viewer_in_workspace_b(conn: asyncpg.Connection, *, member: bool) -> None:
    """Toggle the viewer's Workspace-B membership (the positive-control switch)."""
    if member:
        await conn.execute(
            """
            insert into public.workspace_membership (workspace_id, user_id, role)
            values ($1, $2, 'member')
            on conflict do nothing
            """,
            WORKSPACE_B_ID,
            E6_VIEWER_ID,
        )
    else:
        await conn.execute(
            """
            delete from public.workspace_membership
             where workspace_id = $1 and user_id = $2
            """,
            WORKSPACE_B_ID,
            E6_VIEWER_ID,
        )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class E6Result:
    """Outcome of an E6 run. `to_dict()` is what lands in the result JSON."""

    workspace_b_id: str
    viewer_id: str
    viewer_email: str
    modes: list[str]
    n_questions: int
    n_b_chunks: int
    # negative: per filter -> per mode -> fraction of questions with B-gold recall@10 == 0
    zero_leak_fraction: dict[str, dict[str, float]]
    # sanity: the viewer DOES retrieve A's gold (mean recall@10, pre_filter, per mode)
    viewer_a_gold_recall: dict[str, float]
    # positive control: per mode -> fraction of questions where B-gold surfaces (>0)
    positive_detected_fraction: dict[str, float]
    positive_mean_b_gold_recall: dict[str, float]
    # rows that leaked (empty == clean) — each is {question_id, mode, filter, recall_at_10}
    leaking_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def leak_detected(self) -> bool:
        return bool(self.leaking_rows)

    @property
    def positive_control_ok(self) -> bool:
        # The eval can see B's gold when access is legitimate — so a zero in the
        # negative pass is meaningful, not a structurally blind false pass.
        return any(v > 0.0 for v in self.positive_detected_fraction.values())

    @property
    def passed(self) -> bool:
        return (not self.leak_detected) and self.positive_control_ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_b_id": self.workspace_b_id,
            "viewer_id": self.viewer_id,
            "viewer_email": self.viewer_email,
            "modes": self.modes,
            "n_questions": self.n_questions,
            "n_b_chunks": self.n_b_chunks,
            "zero_leak_fraction": self.zero_leak_fraction,
            "viewer_a_gold_recall": self.viewer_a_gold_recall,
            "positive_control": {
                "detected_fraction": self.positive_detected_fraction,
                "mean_b_gold_recall_at_10": self.positive_mean_b_gold_recall,
                "ok": self.positive_control_ok,
            },
            "leaking_rows": self.leaking_rows,
            "leak_detected": self.leak_detected,
            "passed": self.passed,
        }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_e6(
    *,
    questions: list[dict[str, Any]],
    modes: tuple[str, ...],
    stable_id_map: dict[str, str],
    run_query: RunQuery,
    openai_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    e6_viewer_headers: dict[str, str],
    database_url: str,
) -> E6Result:
    """Seed Workspace B, run the negative leak test + positive control, score.

    `e6_viewer_headers` are the cross-workspace viewer's PostgREST headers
    (built once by the caller — the JWT is the same in both phases; only the DB
    membership row toggles). `run_query` is `runner.run_query`, passed in to keep
    this module free of a circular import.
    """
    sid_to_a_chunk = {sid: cid for cid, sid in stable_id_map.items()}

    conn = await asyncpg.connect(database_url)
    try:
        stable_to_b_chunk = await seed_workspace_b(conn)
        await ensure_e6_viewer(conn)

        # The viewer holds an ACL on BOTH copies of every gold chunk across the
        # whole golden set; membership is then the sole differentiator per row.
        all_gold_stable: set[str] = set()
        for q in questions:
            all_gold_stable.update(q["gold_stable_ids"])
        acl_chunk_ids: set[uuid.UUID] = set()
        for sid in all_gold_stable:
            a_cid = sid_to_a_chunk.get(sid)
            if a_cid is not None:
                acl_chunk_ids.add(uuid.UUID(a_cid))
            b_cid = stable_to_b_chunk.get(sid)
            if b_cid is not None:
                acl_chunk_ids.add(b_cid)
        await set_viewer_acls(conn, acl_chunk_ids)

        all_b_chunk_ids = {str(cid) for cid in stable_to_b_chunk.values()}

        # ---- Negative pass: viewer is NOT a member of B -------------------
        await set_viewer_in_workspace_b(conn, member=False)
        neg_pre: dict[str, list[float]] = {m: [] for m in modes}
        a_gold: dict[str, list[float]] = {m: [] for m in modes}
        neg_pre_rankings: dict[tuple[str, str], list[str]] = {}
        for q in questions:
            qid = q["id"]
            gold = q["gold_stable_ids"]
            b_gold = {str(stable_to_b_chunk[s]) for s in gold if s in stable_to_b_chunk}
            a_gold_ids = {
                sid_to_a_chunk[s] for s in gold if s in sid_to_a_chunk
            }
            for mode in modes:
                rows = await run_query(
                    mode, openai_client, http, supabase_url, e6_viewer_headers,
                    q["question"],
                )
                retrieved = [r.id for r in rows]
                neg_pre_rankings[(qid, mode)] = retrieved
                neg_pre[mode].append(recall_at_10(b_gold, retrieved))
                a_gold[mode].append(recall_at_10(a_gold_ids, retrieved))

        # ---- Positive control: same viewer ADDED to B --------------------
        await set_viewer_in_workspace_b(conn, member=True)
        pos: dict[str, list[float]] = {m: [] for m in modes}
        pos_rankings: dict[tuple[str, str], list[str]] = {}
        for q in questions:
            qid = q["id"]
            gold = q["gold_stable_ids"]
            b_gold = {str(stable_to_b_chunk[s]) for s in gold if s in stable_to_b_chunk}
            for mode in modes:
                rows = await run_query(
                    mode, openai_client, http, supabase_url, e6_viewer_headers,
                    q["question"],
                )
                retrieved = [r.id for r in rows]
                pos_rankings[(qid, mode)] = retrieved
                pos[mode].append(recall_at_10(b_gold, retrieved))
    finally:
        # Always restore the no-member state so a future run's negative pass is
        # honest, then drop the connection.
        try:
            await set_viewer_in_workspace_b(conn, member=False)
        finally:
            await conn.close()

    # ---- Score ----------------------------------------------------------
    leaking_rows: list[dict[str, Any]] = []

    def _fraction_zero(values: list[float]) -> float:
        if not values:
            return 0.0
        return round(sum(1 for v in values if v == 0.0) / len(values), 4)

    zero_leak_fraction: dict[str, dict[str, float]] = {
        "pre_filter": {m: _fraction_zero(neg_pre[m]) for m in modes},
        "post_filter": {},
    }
    # pre_filter leaks: the SQL boundary failed for any row with recall > 0.
    for q in questions:
        qid = q["id"]
        gold = q["gold_stable_ids"]
        b_gold = {str(stable_to_b_chunk[s]) for s in gold if s in stable_to_b_chunk}
        for mode in modes:
            r = recall_at_10(b_gold, neg_pre_rankings[(qid, mode)])
            if r > 0.0:
                leaking_rows.append(
                    {"question_id": qid, "mode": mode, "filter": "pre_filter",
                     "recall_at_10": r}
                )

    # post_filter: take the positive-state ranking (which CONTAINS B's gold) and
    # drop every Workspace-B chunk, modelling the negative-state viewer's
    # visible set. Structurally 0 — verified, not assumed, so a regression that
    # leaves B chunks in after the Python drop still trips the gate.
    post_fracs: dict[str, list[float]] = {m: [] for m in modes}
    for q in questions:
        qid = q["id"]
        gold = q["gold_stable_ids"]
        b_gold = {str(stable_to_b_chunk[s]) for s in gold if s in stable_to_b_chunk}
        for mode in modes:
            kept = [cid for cid in pos_rankings[(qid, mode)] if cid not in all_b_chunk_ids]
            r = recall_at_10(b_gold, kept)
            post_fracs[mode].append(1.0 if r == 0.0 else 0.0)
            if r > 0.0:
                leaking_rows.append(
                    {"question_id": qid, "mode": mode, "filter": "post_filter",
                     "recall_at_10": r}
                )
    zero_leak_fraction["post_filter"] = {
        m: round(sum(post_fracs[m]) / len(post_fracs[m]), 4) if post_fracs[m] else 0.0
        for m in modes
    }

    def _mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    return E6Result(
        workspace_b_id=str(WORKSPACE_B_ID),
        viewer_id=str(E6_VIEWER_ID),
        viewer_email=E6_VIEWER_EMAIL,
        modes=list(modes),
        n_questions=len(questions),
        n_b_chunks=len(stable_to_b_chunk),
        zero_leak_fraction=zero_leak_fraction,
        viewer_a_gold_recall={m: _mean(a_gold[m]) for m in modes},
        positive_detected_fraction={
            m: round(sum(1 for v in pos[m] if v > 0.0) / len(pos[m]), 4) if pos[m] else 0.0
            for m in modes
        },
        positive_mean_b_gold_recall={m: _mean(pos[m]) for m in modes},
        leaking_rows=leaking_rows,
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def render_e6_section(result: E6Result) -> list[str]:
    """Markdown lines for the E6 block of summary.md (security gate, pinned `fail`)."""
    verdict = (
        "PASS — no cross-workspace leak; the positive control confirms B's gold "
        "is retrievable once the viewer joins Workspace B."
        if result.passed
        else (
            "FAIL — cross-workspace LEAK detected (a non-member retrieved Workspace "
            "B's gold)."
            if result.leak_detected
            else "FAIL — positive control retrieved NOTHING; the eval is "
            "structurally blind, so its zero is a false pass."
        )
    )
    lines = [
        "",
        "### E6 (US-009) — second-workspace zero-leak (security invariant, pinned `fail`)",
        "",
        "Cross-workspace viewer is a member of Workspace A with an ACL on both "
        "copies of the gold, but **not** a member of Workspace B. It must retrieve "
        "**0** of B's gold (recall@10 == 0) under both filters and all modes.",
        "",
        "| Mode | Pre zero-leak | Post zero-leak | A-gold recall@10 "
        "| B-gold detected (control) |",
        "|---|---|---|---|---|",
    ]
    for mode in result.modes:
        pre = result.zero_leak_fraction["pre_filter"].get(mode, 0.0)
        post = result.zero_leak_fraction["post_filter"].get(mode, 0.0)
        a_recall = result.viewer_a_gold_recall.get(mode, 0.0)
        pos = result.positive_detected_fraction.get(mode, 0.0)
        lines.append(
            f"| {mode} | {pre:.3f} | {post:.3f} | {a_recall:.3f} | {pos:.3f} |"
        )
    lines += ["", f"**Verdict:** {verdict}"]
    if result.leaking_rows:
        lines += ["", "Leaking rows:"]
        for row in result.leaking_rows[:20]:
            lines.append(
                f"- `{row['question_id']}` {row['mode']}/{row['filter']} "
                f"recall@10={row['recall_at_10']:.3f}"
            )
    return lines
