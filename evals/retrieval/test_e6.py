"""US-009 E6 tests.

Two parts (mirrors db_seed/test_corpus_seed.py):

1. **Offline pure-function checks** that always run — deterministic B-copy
   UUIDs, recall@10 math, the E6Result pass/fail decision logic across the
   three outcomes (clean pass / cross-workspace leak / blind positive control),
   and the markdown rendering.

2. **DB invariant check** that runs only when CORPUS_SEED_DATABASE_URL (or
   DATABASE_URL) is set against a corpus-seeded database: seeding Workspace B is
   idempotent and its chunks carry `stable_id = NULL` (so they stay invisible to
   the E4 stable-id map — the additivity invariant), and re-seeding leaves the
   B chunk count stable.

Run:
    python -m evals.retrieval.test_e6
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
import httpx

import evals.retrieval.e6 as e6
from evals.retrieval.e6 import (
    RETRY_ATTEMPTS,
    E6ExecutionError,
    E6Result,
    _is_transient,
    _retry_transient,
    b_chunk_uuid,
    b_document_uuid,
    recall_at_10,
    render_e6_execution_error,
    render_e6_section,
    seed_workspace_b,
)


def _make_result(
    *,
    pre: dict[str, float],
    post: dict[str, float],
    positive: dict[str, float],
    leaking: list[dict] | None = None,
) -> E6Result:
    modes = list(pre.keys())
    return E6Result(
        workspace_b_id="00000000-0000-0000-0000-0000000000d6",
        viewer_id="viewer",
        viewer_email="v@local.test",
        modes=modes,
        n_questions=50,
        n_b_chunks=120,
        zero_leak_fraction={"pre_filter": pre, "post_filter": post},
        viewer_a_gold_recall={m: 0.67 for m in modes},
        positive_detected_fraction=positive,
        positive_mean_b_gold_recall={m: 0.5 for m in modes},
        leaking_rows=leaking or [],
    )


def _offline_checks() -> None:
    # --- deterministic B UUIDs -------------------------------------------
    assert b_document_uuid("refund-policy") == b_document_uuid("refund-policy")
    assert b_document_uuid("refund-policy") != b_document_uuid("returns-policy")
    assert b_chunk_uuid("refund-policy:0") == b_chunk_uuid("refund-policy:0")
    assert b_chunk_uuid("refund-policy:0") != b_chunk_uuid("refund-policy:1")
    # The B copy's UUID must NOT collide with the corpus seeder's A-side UUID.
    import db_seed.corpus_seed as cs

    assert b_chunk_uuid("refund-policy:0") != cs.chunk_uuid("refund-policy", 0), (
        "B-copy chunk UUID collides with A's corpus chunk UUID"
    )

    # --- recall@10 --------------------------------------------------------
    assert recall_at_10(set(), ["a", "b"]) == 0.0
    assert recall_at_10({"a"}, ["a", "b", "c"]) == 1.0
    assert recall_at_10({"a", "b"}, ["a", "x", "y"]) == 0.5
    assert recall_at_10({"a"}, []) == 0.0
    # Only the top-10 count toward recall.
    top11 = [f"x{i}" for i in range(10)] + ["gold"]
    assert recall_at_10({"gold"}, top11) == 0.0
    top10 = [f"x{i}" for i in range(9)] + ["gold"]
    assert recall_at_10({"gold"}, top10) == 1.0

    # --- E6Result decision logic -----------------------------------------
    modes = ["vector", "keyword", "hybrid"]
    clean = _make_result(
        pre={m: 1.0 for m in modes},
        post={m: 1.0 for m in modes},
        positive={m: 1.0 for m in modes},
    )
    assert clean.passed and not clean.leak_detected and clean.positive_control_ok

    leaked = _make_result(
        pre={"vector": 0.98, "keyword": 1.0, "hybrid": 1.0},
        post={m: 1.0 for m in modes},
        positive={m: 1.0 for m in modes},
        leaking=[{"question_id": "q1", "mode": "vector", "filter": "pre_filter",
                  "recall_at_10": 1.0}],
    )
    assert leaked.leak_detected and not leaked.passed

    blind = _make_result(
        pre={m: 1.0 for m in modes},
        post={m: 1.0 for m in modes},
        positive={m: 0.0 for m in modes},  # positive control found nothing
    )
    assert not blind.leak_detected
    assert not blind.positive_control_ok
    assert not blind.passed, "a blind positive control must fail the run"

    # A single mode detecting B's gold is enough to clear the blindness gate.
    partial_detect = _make_result(
        pre={m: 1.0 for m in modes},
        post={m: 1.0 for m in modes},
        positive={"vector": 0.0, "keyword": 0.0, "hybrid": 0.3},
    )
    assert partial_detect.positive_control_ok and partial_detect.passed

    # --- to_dict shape ----------------------------------------------------
    d = clean.to_dict()
    for key in (
        "workspace_b_id", "zero_leak_fraction", "positive_control",
        "leaking_rows", "leak_detected", "passed",
    ):
        assert key in d, f"to_dict missing {key}"
    assert d["passed"] is True
    assert d["positive_control"]["ok"] is True

    # --- markdown rendering ----------------------------------------------
    pass_md = "\n".join(render_e6_section(clean))
    assert "E6 (US-009)" in pass_md
    assert "PASS" in pass_md and "FAIL" not in pass_md
    leak_md = "\n".join(render_e6_section(leaked))
    assert "FAIL" in leak_md and "LEAK" in leak_md
    assert "q1" in leak_md  # leaking row surfaced
    blind_md = "\n".join(render_e6_section(blind))
    assert "FAIL" in blind_md and "blind" in blind_md

    print("offline checks OK")


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://local.test/rpc/match_chunks")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"{code}", request=req, response=resp)


async def _retry_checks() -> None:
    # Keep the bounded backoff instant so the test stays fast.
    saved = e6.RETRY_INITIAL_DELAY_S
    e6.RETRY_INITIAL_DELAY_S = 0.0
    try:
        # --- transient classification ------------------------------------
        assert _is_transient(asyncio.TimeoutError())
        assert _is_transient(ConnectionError("reset"))
        assert _is_transient(_http_status_error(503))
        assert _is_transient(_http_status_error(429))
        # A deterministic 4xx is NOT a transient blip — it is a real defect.
        assert not _is_transient(_http_status_error(400))
        assert not _is_transient(_http_status_error(404))
        assert not _is_transient(ValueError("bug"))

        # --- retry then succeed ------------------------------------------
        calls = {"n": 0}

        async def flaky_then_ok():
            calls["n"] += 1
            if calls["n"] < 2:
                raise asyncio.TimeoutError()
            return "ok"

        assert await _retry_transient(flaky_then_ok, what="probe") == "ok"
        assert calls["n"] == 2, "should have retried exactly once"

        # --- exhaust retries -> E6ExecutionError (non-blocking signal) ----
        attempts = {"n": 0}

        async def always_transient():
            attempts["n"] += 1
            raise ConnectionError("network down")

        try:
            await _retry_transient(always_transient, what="probe")
        except E6ExecutionError as e:
            assert isinstance(e, RuntimeError)
            assert "probe" in str(e)
        else:
            raise AssertionError("exhausted transient retries must raise E6ExecutionError")
        assert attempts["n"] == RETRY_ATTEMPTS, "should try exactly RETRY_ATTEMPTS times"

        # --- non-transient propagates unchanged (a real bug, not flake) ---
        bug_calls = {"n": 0}

        async def real_bug():
            bug_calls["n"] += 1
            raise ValueError("genuine defect")

        try:
            await _retry_transient(real_bug, what="probe")
        except E6ExecutionError:
            raise AssertionError("a real bug must NOT be softened to E6ExecutionError")
        except ValueError:
            pass
        assert bug_calls["n"] == 1, "non-transient errors must not be retried"

        # --- deterministic 4xx propagates (not retried, not softened) -----
        http_calls = {"n": 0}

        async def bad_request():
            http_calls["n"] += 1
            raise _http_status_error(400)

        try:
            await _retry_transient(bad_request, what="probe")
        except E6ExecutionError:
            raise AssertionError("a 4xx must NOT be softened to E6ExecutionError")
        except httpx.HTTPStatusError:
            pass
        assert http_calls["n"] == 1, "a deterministic 4xx must not be retried"
    finally:
        e6.RETRY_INITIAL_DELAY_S = saved

    # --- execution-error markdown is loud + non-blocking ------------------
    err_md = "\n".join(render_e6_execution_error("rate limit after retries"))
    assert "E6 (US-009)" in err_md
    assert "NOT RUN" in err_md
    assert "non-blocking" in err_md.lower()
    assert "rate limit after retries" in err_md

    print("retry checks OK")


async def _db_invariant() -> None:
    url = os.environ.get("CORPUS_SEED_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("SKIP db invariant: set CORPUS_SEED_DATABASE_URL / DATABASE_URL")
        return
    conn = await asyncpg.connect(url)
    try:
        n_corpus = await conn.fetchval(
            "select count(*) from public.chunks where stable_id is not null"
        )
        if not n_corpus:
            print("SKIP db invariant: corpus not seeded (run db_seed.corpus_seed)")
            return

        mapping_a = await seed_workspace_b(conn)
        mapping_b = await seed_workspace_b(conn)  # idempotent re-seed
        assert set(mapping_a) == set(mapping_b), "B seed not idempotent across runs"

        # Every B chunk must have stable_id NULL — that's what keeps E4's
        # fetch_stable_id_map (and therefore the whole E4 sweep) bit-for-bit.
        n_b_with_stable = await conn.fetchval(
            """
            select count(*)
            from public.chunks c
            join public.documents d on d.id = c.document_id
            where (d.metadata->>'e6_workspace_b') = 'true'
              and c.stable_id is not null
            """
        )
        assert n_b_with_stable == 0, (
            f"{n_b_with_stable} Workspace-B chunks carry a stable_id — "
            "they would pollute the E4 stable-id map and break additivity"
        )
        n_b = await conn.fetchval(
            """
            select count(*)
            from public.chunks c
            join public.documents d on d.id = c.document_id
            where (d.metadata->>'e6_workspace_b') = 'true'
            """
        )
        assert n_b == len(mapping_b), "B chunk count drifted from the mapping"
        print(f"db invariant OK: {n_b} Workspace-B chunks, all stable_id NULL")
    finally:
        # Clean up the B seed so the test leaves no fixtures behind.
        await conn.execute(
            "delete from public.documents where (metadata->>'e6_workspace_b') = 'true'"
        )
        await conn.close()


async def main() -> None:
    _offline_checks()
    await _retry_checks()
    await _db_invariant()


if __name__ == "__main__":
    # Allow `python -m evals.retrieval.test_e6` from the repo root.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    asyncio.run(main())
