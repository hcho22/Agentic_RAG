"""US-069 test: lazy, idempotent per-workspace support-bot provisioning.

Two layers, mirroring `backend/test_supabase_jwt.py`:

1. Unit (always run, no DB / no secrets): the primitive
   (`backend.support_bot.provision_workspace_bot` + helpers) validates its
   `workspace_id`, and fails CLOSED before any network I/O when `SUPABASE_URL` or
   `SUPABASE_SERVICE_ROLE_KEY` is unset — a missing service-role key must never
   reach the wire.

2. Integration (skips cleanly when the local DB / `is_bot` column / Supabase API
   is absent): the PRD US-069 Validation Test, run against a real local Supabase.
   US-072's "issue first key / issue second key" is exercised by calling the
   provisioning primitive directly TWICE (US-072 is not built yet — it will call
   this same primitive). Asserts:
     * before any call, workspace W has ZERO bot rows (no bot at workspace
       creation — a guarded failure indicator);
     * two calls return the SAME id and leave EXACTLY ONE is_bot=true membership
       row, `role='member'` (one bot per workspace, not per key; never role=admin);
     * member-management listings exclude the bot (`where not is_bot`) while an
       unfiltered listing includes it — AC #3, with the human member as the
       positive control proving the filter is what hides the bot;
     * there is no `bot` content-role anywhere — the role CHECK still admits only
       ('admin','member') and the bot is a plain member (AC #4);
     * the DB-layer race guard holds — a raw second is_bot insert for W is
       rejected by the partial unique index (so two concurrent provisions cannot
       create two bots, AC #2 / the sharp-edge constraint).

Run:
    python -m backend.test_us069_bot_provisioning

Requires (integration layer only; unit layer always runs):
    - Local Supabase running (DB + API); DATABASE_URL (or
      PERMISSIONS_TEST_DATABASE_URL) pointing at its DB; the US-069 migration
      (20260624120000_workspace_membership_is_bot.sql) applied.
    - SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY fall back to the well-known local
      defaults. Needs no OpenAI.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import support_bot  # noqa: E402
from support_bot import provision_workspace_bot  # noqa: E402

LOCAL_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
LOCAL_SUPABASE_URL = "http://127.0.0.1:54321"
# Well-known Supabase local-dev service_role key (role=service_role, signed with
# the local default JWT secret). Bypasses RLS and is accepted by the GoTrue admin
# API + PostgREST locally. This is a PUBLIC dev fixture, never a production key.
LOCAL_SERVICE_ROLE_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0."
    "EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU"
)
INSTANCE = "00000000-0000-0000-0000-000000000000"


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key)
    return val if val else default


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# Unit layer — no DB, no secrets.
# --------------------------------------------------------------------------- #


def test_validate_workspace_id() -> int:
    # None / empty / whitespace / non-UUID are rejected; a valid UUID (str or
    # uuid object) is normalized to its canonical string form.
    for bad in (None, "", "   ", "\t\n", "not-a-uuid", "123"):
        try:
            support_bot._validate_workspace_id(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"workspace_id={bad!r} should be rejected")
    good = uuid.uuid4()
    _check(
        support_bot._validate_workspace_id(good) == str(good),
        "uuid object not normalized to str",
    )
    _check(
        support_bot._validate_workspace_id(str(good)) == str(good),
        "uuid str not preserved",
    )
    print("  unit: workspace_id validation (None/empty/non-UUID rejected, UUID normalized)")
    return 1


def test_invalid_workspace_id_rejected_before_secrets() -> int:
    # Validation happens first, so a bad workspace_id raises ValueError even with
    # no SUPABASE_URL / service-role key configured (nothing touches the network).
    saved_url = os.environ.pop("SUPABASE_URL", None)
    saved_key = os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    try:
        asyncio.run(provision_workspace_bot("not-a-uuid"))
    except ValueError:
        ok = True
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected ValueError, got {type(exc).__name__}") from exc
    else:
        ok = False
    finally:
        if saved_url is not None:
            os.environ["SUPABASE_URL"] = saved_url
        if saved_key is not None:
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = saved_key
    _check(ok, "invalid workspace_id did not raise before secret resolution")
    print("  unit: invalid workspace_id rejected before any I/O")
    return 1


def test_missing_service_role_key_fails_closed() -> int:
    # A valid workspace + a SUPABASE_URL (passed as override so the URL check
    # passes) but NO service-role key must fail closed with RuntimeError, before
    # any network call — the key must never reach the wire when unset.
    saved_key = os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    try:
        asyncio.run(
            provision_workspace_bot(str(uuid.uuid4()), supabase_url="http://127.0.0.1:1")
        )
    except RuntimeError as exc:
        ok = "SUPABASE_SERVICE_ROLE_KEY" in str(exc)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected RuntimeError, got {type(exc).__name__}") from exc
    else:
        ok = False
    finally:
        if saved_key is not None:
            os.environ["SUPABASE_SERVICE_ROLE_KEY"] = saved_key
    _check(ok, "missing service-role key did not fail closed with a clear RuntimeError")
    print("  unit: missing SUPABASE_SERVICE_ROLE_KEY fails closed (RuntimeError, no I/O)")
    return 1


def test_missing_supabase_url_fails_closed() -> int:
    saved_url = os.environ.pop("SUPABASE_URL", None)
    try:
        asyncio.run(
            provision_workspace_bot(str(uuid.uuid4()), service_role_key="x")
        )
    except RuntimeError as exc:
        ok = "SUPABASE_URL" in str(exc)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"expected RuntimeError, got {type(exc).__name__}") from exc
    else:
        ok = False
    finally:
        if saved_url is not None:
            os.environ["SUPABASE_URL"] = saved_url
    _check(ok, "missing SUPABASE_URL did not fail closed")
    print("  unit: missing SUPABASE_URL fails closed (RuntimeError)")
    return 1


def test_bot_email_domain_override() -> int:
    saved = os.environ.pop("SUPPORT_BOT_EMAIL_DOMAIN", None)
    try:
        _check(
            support_bot._bot_email_domain() == support_bot._DEFAULT_BOT_EMAIL_DOMAIN,
            "default bot email domain not used when env unset",
        )
        os.environ["SUPPORT_BOT_EMAIL_DOMAIN"] = "custom.example"
        _check(
            support_bot._bot_email_domain() == "custom.example",
            "SUPPORT_BOT_EMAIL_DOMAIN override not honored",
        )
    finally:
        if saved is None:
            os.environ.pop("SUPPORT_BOT_EMAIL_DOMAIN", None)
        else:
            os.environ["SUPPORT_BOT_EMAIL_DOMAIN"] = saved
    print("  unit: bot email domain default + override")
    return 1


def run_unit_layer() -> int:
    total = 0
    total += test_validate_workspace_id()
    total += test_invalid_workspace_id_rejected_before_secrets()
    total += test_missing_service_role_key_fails_closed()
    total += test_missing_supabase_url_fails_closed()
    total += test_bot_email_domain_override()
    return total


# --------------------------------------------------------------------------- #
# Integration layer — real local Supabase; skips cleanly when absent.
# --------------------------------------------------------------------------- #


class Fixture:
    def __init__(self) -> None:
        self.ws = str(uuid.uuid4())          # workspace W
        self.human = str(uuid.uuid4())       # a human member of W (positive control)
        self.extra = str(uuid.uuid4())       # spare auth.users row for the race-guard probe
        self.bot_id: str | None = None       # set after provisioning (for cleanup)


async def _column_exists(conn: asyncpg.Connection, table: str, column: str) -> bool:
    return bool(
        await conn.fetchval(
            """
            select 1 from information_schema.columns
            where table_schema = 'public' and table_name = $1 and column_name = $2
            """,
            table,
            column,
        )
    )


async def _seed(conn: asyncpg.Connection, fx: Fixture) -> None:
    await conn.execute(
        """
        insert into auth.users
          (id, instance_id, email, encrypted_password, aud, role,
           raw_app_meta_data, raw_user_meta_data,
           created_at, updated_at, email_confirmed_at)
        values
          ($1, $3, $4, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now()),
          ($2, $3, $5, '', 'authenticated', 'authenticated',
           '{}'::jsonb, '{}'::jsonb, now(), now(), now())
        """,
        fx.human, fx.extra, INSTANCE,
        f"human-{fx.human[:8]}@test.local", f"extra-{fx.extra[:8]}@test.local",
    )
    await conn.execute(
        "insert into public.workspaces (id, name) values ($1, 'US069-W')", fx.ws
    )
    # One human member, no bot. The bot must be created lazily by the primitive,
    # never at workspace-creation time.
    await conn.execute(
        """
        insert into public.workspace_membership (workspace_id, user_id, role)
        values ($1, $2, 'member')
        """,
        fx.ws, fx.human,
    )


async def _cleanup(conn: asyncpg.Connection, fx: Fixture) -> None:
    # Deleting the workspace cascades all membership rows (human + bot). The
    # auth.users rows are independent, so drop them explicitly: the GoTrue-created
    # bot row, plus the two seeded humans.
    await conn.execute("delete from public.workspaces where id = $1::uuid", fx.ws)
    ids = [fx.human, fx.extra]
    if fx.bot_id is not None:
        ids.append(fx.bot_id)
    await conn.execute("delete from auth.users where id = any($1::uuid[])", ids)


async def _bot_rows(conn: asyncpg.Connection, ws: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "select user_id, role, is_bot from public.workspace_membership "
        "where workspace_id = $1::uuid and is_bot",
        ws,
    )


async def _role_check_admits_only_admin_member(conn: asyncpg.Connection) -> None:
    """AC #4: no `bot` content-role exists — the role CHECK still admits exactly
    ('admin','member'), so 'bot' is not (and cannot become) a role value."""
    defs = await conn.fetch(
        """
        select pg_get_constraintdef(c.oid) as def
        from pg_constraint c
        join pg_class t on t.oid = c.conrelid
        join pg_namespace n on n.oid = t.relnamespace
        where n.nspname = 'public' and t.relname = 'workspace_membership'
          and c.contype = 'c' and pg_get_constraintdef(c.oid) ilike '%role%'
        """
    )
    _check(bool(defs), "no role CHECK constraint found on workspace_membership")
    joined = " ".join(d["def"].lower() for d in defs)
    _check("admin" in joined and "member" in joined, f"role CHECK unexpected: {joined}")
    _check("'bot'" not in joined, f"a 'bot' role leaked into the role CHECK: {joined}")
    # And no membership row anywhere carries a role outside the administrative set.
    stray = await conn.fetchval(
        "select count(*) from public.workspace_membership "
        "where role not in ('admin','member')"
    )
    _check(stray == 0, f"{stray} membership rows have a non-administrative role")


async def _run_integration() -> int | None:
    db_url = _env("PERMISSIONS_TEST_DATABASE_URL") or _env("DATABASE_URL") or LOCAL_DB_URL
    try:
        conn = await asyncpg.connect(db_url)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"SKIP integration: cannot connect to {db_url} ({exc})")
        return None

    try:
        if not await _column_exists(conn, "workspace_membership", "is_bot"):
            print("SKIP integration: workspace_membership.is_bot absent (US-069 migration not applied)")
            return None

        supabase_url = _env("SUPABASE_URL", LOCAL_SUPABASE_URL)
        service_role_key = _env("SUPABASE_SERVICE_ROLE_KEY", LOCAL_SERVICE_ROLE_KEY)
        assert supabase_url and service_role_key  # for the type checker

        fx = Fixture()
        await _seed(conn, fx)
        total = 0
        try:
            # No bot before any provisioning call (failure indicator: a bot born
            # at workspace creation).
            _check(await _bot_rows(conn, fx.ws) == [], "a bot existed before provisioning")
            total += 1
            print("  integration: workspace has no bot before provisioning")

            # PRD steps 1+2: "issue first key / issue second key" -> call the
            # primitive twice. (US-072 will be the real caller; it invokes exactly
            # this primitive.) Skip cleanly if the Supabase API is down.
            try:
                async with httpx.AsyncClient(timeout=10.0) as http:
                    id1 = await provision_workspace_bot(
                        fx.ws,
                        http=http,
                        supabase_url=supabase_url,
                        service_role_key=service_role_key,
                    )
                    fx.bot_id = id1
                    id2 = await provision_workspace_bot(
                        fx.ws,
                        http=http,
                        supabase_url=supabase_url,
                        service_role_key=service_role_key,
                    )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                print(f"SKIP integration: Supabase API unreachable ({exc})")
                return None

            # Idempotent: same id back, and EXACTLY ONE is_bot row.
            _check(id1 == id2, f"second provision returned a different bot id: {id1} != {id2}")
            total += 1
            print(f"  integration: two calls returned the same bot id ({id1[:8]}…)")

            rows = await _bot_rows(conn, fx.ws)
            _check(len(rows) == 1, f"expected exactly ONE bot row, found {len(rows)}")
            _check(str(rows[0]["user_id"]) == id1, "bot row user_id != returned id")
            _check(rows[0]["role"] == "member", f"bot role must be 'member', got {rows[0]['role']!r}")
            total += 1
            print("  integration: exactly one is_bot row, role='member', matches returned id")

            # AC #3: member-management listing excludes the bot; unfiltered includes
            # it (the human member is the positive control proving the filter works).
            non_bot = {
                str(r["user_id"])
                for r in await conn.fetch(
                    "select user_id from public.workspace_membership "
                    "where workspace_id = $1::uuid and not is_bot",
                    fx.ws,
                )
            }
            all_members = {
                str(r["user_id"])
                for r in await conn.fetch(
                    "select user_id from public.workspace_membership where workspace_id = $1::uuid",
                    fx.ws,
                )
            }
            _check(fx.human in non_bot, "human member missing from the member listing")
            _check(id1 not in non_bot, "bot leaked into the member-management listing")
            _check(id1 in all_members and fx.human in all_members, "unfiltered listing wrong")
            total += 1
            print("  integration: member-listing filter excludes the bot, keeps the human")

            # AC #4: no `bot` content-role anywhere.
            await _role_check_admits_only_admin_member(conn)
            total += 1
            print("  integration: no 'bot' content-role (role CHECK admits only admin/member)")

            # AC #2 sharp edge: the partial unique index prevents a SECOND bot for
            # the workspace even via a raw insert (so concurrent provisions cannot
            # create two bots). Use the spare auth.users row to satisfy the FK.
            try:
                await conn.execute(
                    "insert into public.workspace_membership (workspace_id, user_id, role, is_bot) "
                    "values ($1::uuid, $2::uuid, 'member', true)",
                    fx.ws, fx.extra,
                )
            except asyncpg.exceptions.UniqueViolationError:
                pass
            else:
                raise AssertionError(
                    "a SECOND is_bot row was accepted — partial unique index missing"
                )
            total += 1
            print("  integration: DB-layer race guard holds (second bot insert rejected)")
        finally:
            await _cleanup(conn, fx)
        return total
    finally:
        await conn.close()


def _run() -> None:
    unit_total = run_unit_layer()
    print(f"OK: US-069 unit layer — {unit_total} assertions on the provisioning primitive")

    integ_total = asyncio.run(_run_integration())
    if integ_total is None:
        print(
            "OK: US-069 — unit layer passed; integration layer skipped "
            "(no local Supabase / is_bot column / API)"
        )
    else:
        print(
            f"OK: US-069 — unit + integration passed ({unit_total + integ_total} "
            "assertions); the bot is provisioned lazily + idempotently (one bot per "
            "workspace, role=member), excluded from member listings, no bot content-role"
        )


if __name__ == "__main__":
    _run()
