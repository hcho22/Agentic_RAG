"""US-067 integration test: conversation status machine + escalation latch.

Exercises the DB-level state machine installed by
`supabase/migrations/20260623130000_conversation_status_machine.sql` directly
against the local Postgres, because the invariant under test is enforced by a
BEFORE UPDATE trigger - it must hold for *every* writer (bot pipeline, support
agent, raw SQL), not just for one service-layer code path. asyncpg connects as
the `postgres` superuser; triggers (unlike RLS) are not bypassed by superusers,
so the guard fires exactly as it would in production.

Mirrors the PRD US-067 "Validation Test":
  1. Conversation C in `active`; transition to `escalated`, record escalated_at=t0.
  2. A second escalate write must NOT move escalated_at (stays t0).
  3. `escalated -> active` is rejected (no de-escalation).
  4. Transition to `resolved`; a subsequent `resolved -> escalated` (and any
     `resolved -> *`) is rejected (resolved is terminal).
Plus the derivable-deflection corollary: a conversation taken `active -> resolved`
directly keeps escalated_at NULL, so the documented deflection-rate snippet
counts it as deflected.

The guard fires BEFORE INSERT OR UPDATE, so the create path is exercised too: an
INSERT born `escalated` is latched, while one born `active`/`resolved` has its
escalated_at forced NULL even when the caller supplies a stray timestamp - the
trigger is the sole author of the latch on both paths.

Run:
    python -m backend.test_conversation_status_machine

Requires:
    - Local Supabase running (DB at postgresql://postgres:postgres@127.0.0.1:54322/postgres).
    - CONVERSATION_TEST_DATABASE_URL or DATABASE_URL pointing at that DB; falls
      back to the well-known local default.
    - US-066's `conversations` table migration applied.

Skips cleanly when the DB is unreachable or the `conversations` table is absent
(US-066 not yet applied).
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import asyncpg

# Allow `python -m backend.test_conversation_status_machine` from the repo root.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

LOCAL_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
DEFAULT_WORKSPACE_ID = "00000000-0000-0000-0000-0000000000d0"


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(
        await conn.fetchval("select to_regclass($1)", f"public.{table}")
    )


async def _insert_conversation(conn: asyncpg.Connection) -> str:
    """Insert a fresh `active` conversation; return its id."""
    conv_id = str(uuid.uuid4())
    await conn.execute(
        """
        insert into public.conversations (id, workspace_id)
        values ($1::uuid, $2::uuid)
        """,
        conv_id, DEFAULT_WORKSPACE_ID,
    )
    return conv_id


async def _insert_with(
    conn: asyncpg.Connection, status: str, escalated_at: object = None
) -> str:
    """Insert a conversation with an explicit status (and a possibly-stray
    escalated_at) to exercise the INSERT branch of the guard; return its id."""
    conv_id = str(uuid.uuid4())
    await conn.execute(
        """
        insert into public.conversations (id, workspace_id, status, escalated_at)
        values ($1::uuid, $2::uuid, $3, $4)
        """,
        conv_id, DEFAULT_WORKSPACE_ID, status, escalated_at,
    )
    return conv_id


async def _status_and_latch(
    conn: asyncpg.Connection, conv_id: str
) -> tuple[str, object]:
    row = await conn.fetchrow(
        "select status, escalated_at from public.conversations where id = $1::uuid",
        conv_id,
    )
    return row["status"], row["escalated_at"]


async def _expect_rejected(coro, label: str, *, exc_type=asyncpg.PostgresError) -> None:
    """Assert that an illegal UPDATE raises the expected Postgres error class."""
    try:
        await coro
    except exc_type as exc:
        print(f"  rejected as expected: {label} ({type(exc).__name__})")
        return
    except asyncpg.PostgresError as exc:  # wrong-but-still-rejected: surface it
        raise AssertionError(
            f"{label} was rejected by {type(exc).__name__}, expected {exc_type.__name__}"
        ) from exc
    raise AssertionError(f"{label} should have been rejected but succeeded")


async def _run() -> None:
    db_url = (
        _env("CONVERSATION_TEST_DATABASE_URL")
        or _env("DATABASE_URL")
        or LOCAL_DB_URL
    )

    try:
        conn = await asyncpg.connect(db_url)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"SKIP: cannot connect to {db_url} ({exc})")
        return

    created: list[str] = []
    try:
        if not await _table_exists(conn, "conversations"):
            print("SKIP: public.conversations absent (US-066 not yet applied)")
            return

        # --- PRD Validation Test --------------------------------------------
        conv = await _insert_conversation(conn)
        created.append(conv)

        status, latch = await _status_and_latch(conn, conv)
        assert status == "active", f"new conversation should be active, got {status!r}"
        assert latch is None, f"new conversation should have null escalated_at, got {latch!r}"

        # 1. active -> escalated stamps escalated_at = t0.
        await conn.execute(
            "update public.conversations set status = 'escalated' where id = $1::uuid",
            conv,
        )
        status, t0 = await _status_and_latch(conn, conv)
        assert status == "escalated", f"expected escalated, got {status!r}"
        assert t0 is not None, "escalated_at must be stamped on first escalate"
        print(f"  active -> escalated latched escalated_at = {t0}")

        # 2. A second escalate write must NOT move escalated_at. We even try to
        #    force a different value; the set-once latch must ignore it.
        await conn.execute(
            """
            update public.conversations
               set status = 'escalated', escalated_at = now() + interval '1 hour'
             where id = $1::uuid
            """,
            conv,
        )
        status, t1 = await _status_and_latch(conn, conv)
        assert status == "escalated", f"expected escalated, got {status!r}"
        assert t1 == t0, f"escalated_at moved on second escalate: {t0!r} -> {t1!r}"
        print("  second escalate write did NOT move escalated_at (set-once latch holds)")

        # 3. escalated -> active is rejected (no de-escalation).
        await _expect_rejected(
            conn.execute(
                "update public.conversations set status = 'active' where id = $1::uuid",
                conv,
            ),
            "escalated -> active",
            exc_type=asyncpg.exceptions.RaiseError,
        )
        status, latch = await _status_and_latch(conn, conv)
        assert status == "escalated" and latch == t0, "row mutated by rejected de-escalation"

        # 4. escalated -> resolved is allowed; latch survives into resolved.
        await conn.execute(
            "update public.conversations set status = 'resolved' where id = $1::uuid",
            conv,
        )
        status, t_resolved = await _status_and_latch(conn, conv)
        assert status == "resolved", f"expected resolved, got {status!r}"
        assert t_resolved == t0, f"latch must survive into resolved: {t0!r} -> {t_resolved!r}"

        # ...and resolved is terminal: any transition out of it is rejected.
        await _expect_rejected(
            conn.execute(
                "update public.conversations set status = 'escalated' where id = $1::uuid",
                conv,
            ),
            "resolved -> escalated",
            exc_type=asyncpg.exceptions.RaiseError,
        )
        await _expect_rejected(
            conn.execute(
                "update public.conversations set status = 'active' where id = $1::uuid",
                conv,
            ),
            "resolved -> active",
            exc_type=asyncpg.exceptions.RaiseError,
        )
        status, latch = await _status_and_latch(conn, conv)
        assert status == "resolved" and latch == t0, "row mutated by rejected terminal transition"

        # --- Derivable-deflection corollary ---------------------------------
        # active -> resolved directly never sets escalated_at, so the deflection
        # snippet counts it as deflected (resolved AND escalated_at IS NULL).
        deflected = await _insert_conversation(conn)
        created.append(deflected)
        await conn.execute(
            "update public.conversations set status = 'resolved' where id = $1::uuid",
            deflected,
        )
        status, latch = await _status_and_latch(conn, deflected)
        assert status == "resolved", f"expected resolved, got {status!r}"
        assert latch is None, f"direct active->resolved must keep escalated_at null, got {latch!r}"

        # The two rows we created: one human-handled (escalated), one deflected.
        rate = await conn.fetchval(
            """
            select
              count(*) filter (where status = 'resolved' and escalated_at is null)::numeric
              / nullif(count(*) filter (where status = 'resolved'), 0)
            from public.conversations
            where id = any($1::uuid[])
            """,
            created,
        )
        assert rate == 0.5, f"deflection rate over the 2 test rows should be 0.5, got {rate!r}"
        print(f"  derived deflection rate over test rows = {rate}")

        # --- INSERT-path latch (create path is trigger-owned too) -----------
        # The guard fires BEFORE INSERT OR UPDATE, so escalated_at is owned by
        # the trigger on the create path as well: a direct INSERT cannot plant a
        # value the deflection metric would misread.
        stray = await conn.fetchval("select now() + interval '1 hour'")

        # (a) born 'escalated' -> escalated_at stamped non-null.
        ins_escalated = await _insert_with(conn, "escalated", stray)
        created.append(ins_escalated)
        status, latch = await _status_and_latch(conn, ins_escalated)
        assert status == "escalated", f"expected escalated, got {status!r}"
        assert latch is not None, "INSERT status='escalated' must stamp escalated_at"

        # (b) born 'active' with a stray escalated_at supplied -> forced null.
        ins_active = await _insert_with(conn, "active", stray)
        created.append(ins_active)
        status, latch = await _status_and_latch(conn, ins_active)
        assert status == "active", f"expected active, got {status!r}"
        assert latch is None, f"INSERT must ignore a stray escalated_at, got {latch!r}"

        # (c) born 'resolved' (never escalated) with a stray escalated_at ->
        #     forced null, so the deflection snippet counts it as deflected.
        ins_resolved = await _insert_with(conn, "resolved", stray)
        created.append(ins_resolved)
        status, latch = await _status_and_latch(conn, ins_resolved)
        assert status == "resolved", f"expected resolved, got {status!r}"
        assert latch is None, f"INSERT status='resolved' must keep escalated_at null, got {latch!r}"
        ins_rate = await conn.fetchval(
            """
            select
              count(*) filter (where status = 'resolved' and escalated_at is null)::numeric
              / nullif(count(*) filter (where status = 'resolved'), 0)
            from public.conversations
            where id = $1::uuid
            """,
            ins_resolved,
        )
        assert ins_rate == 1, f"inserted resolved+null row must count as deflected, got {ins_rate!r}"
        print("  INSERT-path latch holds (escalated stamps, active/resolved forced null)")

        # --- CHECK constraint rejects an out-of-set status ------------------
        # Run on a fresh `active` row: from `active` the trigger passes a bogus
        # value straight through (it only guards de-escalation / terminal exits),
        # so the rejection here is the CHECK constraint itself, not the trigger.
        check_conv = await _insert_conversation(conn)
        created.append(check_conv)
        await _expect_rejected(
            conn.execute(
                "update public.conversations set status = 'bogus' where id = $1::uuid",
                check_conv,
            ),
            "status = 'bogus' (CHECK constraint)",
            exc_type=asyncpg.exceptions.CheckViolationError,
        )

        print(
            "OK: status machine verified - active->escalated->resolved latch, "
            "set-once escalated_at on INSERT + UPDATE, de-escalation + terminal "
            "transitions rejected, deflection derivable, status CHECK enforced"
        )
    finally:
        if created:
            await conn.execute(
                "delete from public.conversations where id = any($1::uuid[])",
                created,
            )
        await conn.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
