"""Per-workspace support bot: provisioning (US-069) and per-turn retrieval (US-070).

This module owns the support bot's whole server-side lifecycle. Both halves lean
on ADR-0008's principle that the bot is an ordinary authenticated *member*, not a
new content role:

  * PROVISIONING (US-069) — `provision_workspace_bot(workspace_id)` lazily and
    idempotently creates the bot: an ordinary `auth.users` row plus a
    `workspace_membership(role='member', is_bot=true)` row, exactly one per
    workspace. `is_bot` is a FLAG, not a role — like `workspace_membership.role`
    it is administrative metadata that never enters any visibility/retrieval
    predicate (ADR-0002). Creating the `auth.users` row requires the service-role
    key (GoTrue admin API); that key bypasses RLS and is strictly server-side.

  * RETRIEVAL (US-070) — `run_bot_deflection_turn(...)` runs one customer turn's
    deflection pipeline AS that bot: mint a ~60s Supabase-compatible JWT
    (`sub = bot_user_id`, US-068), call `match_chunks`/`keyword_search` with it so
    `auth.uid()` resolves to the bot and the existing membership + owner-OR-ACL
    boundary applies WHOLESALE, then discard the token. The bot therefore answers
    ONLY from documents shared to it via `chunk_acl` (share-to-bot) — there is NO
    new retrieval predicate and NO `principal_ids` passed from the backend.

The two halves meet at `conversations.bot_user_id`: provisioning returns that id;
retrieval mints a token for it each turn. The minter (US-068 `mint_supabase_jwt`)
is dependency-INJECTED into the retrieval path rather than imported, to avoid a
`main.py` ⇄ `support_bot.py` import cycle and to keep the retrieval seam pure and
unit-testable with no JWT secret; the real call site (US-079 `_run_widget_bot_turn`) passes it in.

SECURITY (shared): two distinct server-side-only secrets pass through this module
and MUST NEVER be logged, returned, or sent client-side — the service-role key
(provisioning; bypasses RLS, can forge any identity) and the minted bot JWT
(retrieval; a bearer credential equivalent to a logged-in session). Neither is
ever built into an error message from request headers; the only field a caller
may surface to the customer is `DeflectionResult.customer_message`.

Tests: `python -m backend.test_us069_bot_provisioning`,
`python -m backend.test_us070_bot_retrieval`,
`python -m backend.test_us070_bot_retrieval_integration`.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Callable

import httpx
from openai import AsyncOpenAI

from escalation import DeflectionResult, EscalationConfig, run_deflection_pipeline
from retrieval import DEFAULT_TOP_K

log = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# US-069: lazy, idempotent per-workspace support-bot provisioning (ADR-0008).
#
# `provision_workspace_bot` is the SINGLE place the backend creates the bot.
# Lazy — runs only when a caller invokes it (US-072 calls it on first widget-key
# issuance); nothing provisions a bot at workspace-creation time, so a
# knowledge-assistant-only deployment never spawns one. Idempotent / exactly one
# bot per workspace (not per key): the hard guarantee is the partial unique index
# `workspace_membership_one_bot_per_workspace` (where is_bot) from
# 20260624120000_workspace_membership_is_bot.sql — even two *concurrent* first-
# time provisions cannot create two bots; the loser gets a unique violation,
# drops the orphan `auth.users` row it just made, and returns the winner's bot.
# The app-level fast-path is an optimization on top of that DB guard, never the
# sole defense (the constraint must NOT be "check-then-insert").
# -----------------------------------------------------------------------------

# Default lifetime for the per-provision HTTP client when the caller passes none.
_DEFAULT_TIMEOUT = 10.0

# Internal, non-routable email domain for the bot's auth.users row. The row is
# admin-created with email_confirm=true and no password, so the address never
# logs in or receives mail — it exists only to satisfy GoTrue's createUser
# contract. Overridable so a deployment can scope bot emails to its own domain.
_DEFAULT_BOT_EMAIL_DOMAIN = "bots.support.internal"


def _bot_email_domain() -> str:
    return os.environ.get("SUPPORT_BOT_EMAIL_DOMAIN") or _DEFAULT_BOT_EMAIL_DOMAIN


def _resolve_supabase_url(url: str | None) -> str:
    resolved = url if url is not None else os.environ.get("SUPABASE_URL")
    if not resolved:
        raise RuntimeError(
            "SUPABASE_URL is not configured. It is required to provision the "
            "per-workspace support bot (US-069). Set it on the deploy target."
        )
    return resolved.rstrip("/")


def _resolve_service_role_key(key: str | None) -> str:
    """Return the service-role key, fail-closed.

    Resolved at call time (not import) so a deployment that never enables support
    need not set it; `key` is a test-only override. The key bypasses RLS — it is
    server-side only and must never be logged, returned, or sent client-side.
    """
    resolved = key if key is not None else os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not resolved:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured. It is required to "
            "create the support bot's auth.users row via the GoTrue admin API "
            "(US-069). Set it to the project's service-role key on the deploy "
            "target. It bypasses RLS — keep it server-side, never client-side."
        )
    return resolved


def _validate_workspace_id(workspace_id: object) -> str:
    """Normalize and validate the workspace id.

    Must be a well-formed UUID (it is a FK to public.workspaces). Validating here
    fails fast on garbage instead of sending it to PostgREST, and keeps the
    primitive from minting a bot for a bogus/forged target.
    """
    if workspace_id is None:
        raise ValueError("workspace_id must not be None")
    ws_str = str(workspace_id).strip()
    if not ws_str:
        raise ValueError("workspace_id must not be empty or whitespace-only")
    try:
        return str(uuid.UUID(ws_str))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"workspace_id must be a valid UUID: {workspace_id!r}") from exc


def _service_role_headers(key: str) -> dict[str, str]:
    """Headers that authenticate as the service role (bypassing RLS).

    The key never appears in any error this module raises — callers build error
    messages from response bodies (which never contain the key), never headers.
    """
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _provisioning_error(action: str, response: httpx.Response) -> RuntimeError:
    """Build a safe error: status + a truncated body snippet, never the key/headers."""
    snippet = (response.text or "")[:300]
    return RuntimeError(
        f"support-bot provisioning failed during {action}: "
        f"HTTP {response.status_code} {snippet}"
    )


async def _find_existing_bot(
    http: httpx.AsyncClient, base_url: str, headers: dict[str, str], workspace_id: str
) -> str | None:
    """Return the existing bot's user id for the workspace, or None.

    This is the idempotency fast-path: the is_bot membership row is the single
    source of truth for "which auth.users id is this workspace's bot".
    """
    r = await http.get(
        f"{base_url}/rest/v1/workspace_membership",
        params={
            "workspace_id": f"eq.{workspace_id}",
            "is_bot": "eq.true",
            "select": "user_id",
            "limit": "1",
        },
        headers=headers,
    )
    if r.status_code != 200:
        raise _provisioning_error("existing-bot lookup", r)
    rows = r.json()
    if rows:
        return str(rows[0]["user_id"])
    return None


async def _create_bot_user(
    http: httpx.AsyncClient, base_url: str, headers: dict[str, str], workspace_id: str
) -> str:
    """Create the bot's auth.users row via the GoTrue admin API; return its id.

    A fully-random email guarantees no collision with any prior/orphaned bot row,
    so concurrency never trips a duplicate-email error here — the membership
    partial unique index (not the email) is the one-bot-per-workspace guard. The
    workspace association is recorded in app_metadata (admin-controlled, not
    user-writable) for traceability; it carries no authorization meaning.
    """
    email = f"support-bot-{uuid.uuid4().hex}@{_bot_email_domain()}"
    r = await http.post(
        f"{base_url}/auth/v1/admin/users",
        headers=headers,
        json={
            "email": email,
            "email_confirm": True,
            "app_metadata": {"is_support_bot": True, "workspace_id": workspace_id},
            "user_metadata": {"display_name": "Support Bot"},
        },
    )
    if r.status_code not in (200, 201):
        raise _provisioning_error("auth.users create", r)
    return str(r.json()["id"])


async def _insert_bot_membership(
    http: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    workspace_id: str,
    bot_id: str,
) -> bool:
    """Insert the bot's membership row. Return True if inserted, False on conflict.

    A unique-violation conflict (SQLSTATE 23505) means another provision won the
    race for this workspace's single bot slot (the partial unique index) — a
    normal, expected outcome the caller resolves by returning the winner. Any
    other non-2xx (including a 409 that is NOT a 23505 — e.g. a 23503 FK violation
    from a nonexistent workspace) is a real failure and raises.
    """
    r = await http.post(
        f"{base_url}/rest/v1/workspace_membership",
        headers={**headers, "Prefer": "return=minimal"},
        json={
            "workspace_id": workspace_id,
            "user_id": bot_id,
            "role": "member",
            "is_bot": True,
        },
    )
    if r.status_code in (200, 201, 204):
        return True
    # Only a 23505 unique violation is a one-bot-per-workspace race loss. PostgREST
    # also returns 409 for other constraint failures (e.g. a 23503 FK violation
    # when the workspace does not exist), so gate on the SQLSTATE marker — never the
    # bare 409 — and let everything else surface as a clear provisioning error.
    if "23505" in (r.text or ""):
        return False
    raise _provisioning_error("membership insert", r)


async def _delete_bot_user(
    http: httpx.AsyncClient, base_url: str, headers: dict[str, str], bot_id: str
) -> None:
    """Best-effort delete of an orphan bot auth.users row (no membership attached).

    Called when the membership insert lost the race or failed: the auth.users row
    we created would otherwise dangle with no membership (hence no access — not a
    security issue, just untidy). Failures here are swallowed; a stray row is
    harmless and the next provision uses the row that actually has membership.
    """
    try:
        r = await http.delete(
            f"{base_url}/auth/v1/admin/users/{bot_id}", headers=headers
        )
        r.raise_for_status()
    except httpx.HTTPError:
        log.warning("support-bot orphan cleanup failed for user %s (harmless)", bot_id)


async def provision_workspace_bot(
    workspace_id: str,
    *,
    http: httpx.AsyncClient | None = None,
    supabase_url: str | None = None,
    service_role_key: str | None = None,
) -> str:
    """Lazily and idempotently provision the per-workspace support bot.

    Creates (or returns the existing) bot `auth.users` row +
    `workspace_membership(role='member', is_bot=true)` row for `workspace_id` and
    returns the bot's user id (what populates `conversations.bot_user_id`).
    Exactly one bot per workspace — a second call returns the same id and creates
    no second row (guaranteed by the partial unique index, race-safe).

    Args:
        workspace_id: the workspace to provision the bot for. Must be a valid UUID
            referencing public.workspaces.
        http: optional shared `httpx.AsyncClient`; one is created and closed per
            call when omitted.
        supabase_url / service_role_key: test-only overrides; production reads
            `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` from the environment.

    Returns:
        The bot's `auth.users` id as a string.

    Raises:
        ValueError: `workspace_id` is not a valid UUID.
        RuntimeError: `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` is unset, or a
            provisioning HTTP call fails.
    """
    ws = _validate_workspace_id(workspace_id)
    base_url = _resolve_supabase_url(supabase_url)
    # Fail-closed on the secret BEFORE any I/O so a missing key never reaches the
    # network and is reported as a clear configuration error.
    key = _resolve_service_role_key(service_role_key)
    headers = _service_role_headers(key)

    own_client = http is None
    client = http if http is not None else httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    try:
        # 1. Idempotency fast-path: a bot already exists for this workspace.
        existing = await _find_existing_bot(client, base_url, headers, ws)
        if existing is not None:
            return existing

        # 2. Create the bot's auth.users row.
        bot_id = await _create_bot_user(client, base_url, headers, ws)

        # 3. Attach membership. The partial unique index is the race guard, so a
        #    lost race surfaces here as a conflict (or a hard error on anything
        #    else). Either way we drop the auth.users row we just created so it
        #    does not dangle.
        try:
            inserted = await _insert_bot_membership(client, base_url, headers, ws, bot_id)
        except Exception:
            await _delete_bot_user(client, base_url, headers, bot_id)
            raise
        if inserted:
            return bot_id

        # Lost the race: another concurrent provision created the one bot. Drop
        # our orphan auth.users row and return the winner.
        await _delete_bot_user(client, base_url, headers, bot_id)
        winner = await _find_existing_bot(client, base_url, headers, ws)
        if winner is None:
            raise RuntimeError(
                "support-bot membership conflicted but no existing bot was found "
                f"for workspace {ws} (unexpected partial state)"
            )
        return winner
    finally:
        if own_client:
            await client.aclose()


# -----------------------------------------------------------------------------
# US-070: support-bot retrieval — per-turn bot token calls match_chunks as the bot.
#
# Runs the ADR-0003 deflection pipeline AS the bot provisioned above: mint a ~60s
# Supabase-compatible JWT (US-068), retrieve with it so the existing membership +
# owner-OR-ACL boundary resolves the bot from `auth.uid()` wholesale, discard.
# The bot token is a new *issuer* beside GoTrue, not a new enforcement path — on
# the wire it is an ordinary `role=authenticated` JWT, so a backend that forgets
# the active-workspace filter can only under-return within the bot's own
# membership, never leak across the tenant boundary.
# -----------------------------------------------------------------------------

# ~60s: long enough for one turn's retrieval round trip, short enough that a
# leaked token (which, per the threat model, this module is built to prevent
# leaking at all) is near-useless. Minted fresh PER TURN and discarded — never
# cached across turns (US-070).
BOT_TOKEN_TTL_SECONDS = 60

# (sub, ttl_seconds) -> compact JWT string. This is exactly the shape of US-068's
# `mint_supabase_jwt`; injected so this module needs neither the signing secret
# nor an import of `main.py` (see the module docstring).
MintToken = Callable[[str, int], str]


def build_bot_supabase_headers(bot_token: str, anon_key: str) -> dict[str, str]:
    """Supabase/PostgREST headers carrying the bot's minted JWT as the Bearer.

    Identical in shape to `main._supabase_headers` for a human user: `apikey` is
    the public anon key (the PostgREST gateway key, NOT an identity), and the
    `Authorization: Bearer <jwt>` is what actually carries identity — here the
    bot's short-lived self-signed token. PostgREST verifies the JWT with the
    project secret and resolves `auth.uid()` to the bot's `sub`, so RLS treats
    the bot exactly like any `authenticated` member.
    """
    return {
        "apikey": anon_key,
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def run_bot_deflection_turn(
    *,
    mint_token: MintToken,
    anon_key: str,
    bot_user_id: str,
    workspace_id: str,
    embedder_client: AsyncOpenAI,
    answerer_client: AsyncOpenAI,
    judge_client: AsyncOpenAI,
    http: httpx.AsyncClient,
    supabase_url: str,
    message: str,
    config: EscalationConfig,
    match_threshold: float,
    top_k: int = DEFAULT_TOP_K,
    answerer_model: str | None = None,
    judge_model: str | None = None,
) -> DeflectionResult:
    """Run one customer turn's deflection pipeline AS the support bot (US-070).

    Mints a fresh ~60s bot JWT for `bot_user_id` (via the injected `mint_token`),
    runs the ADR-0003 deflection pipeline with that JWT in the Supabase headers so
    `match_chunks`/`keyword_search` execute as the bot, and passes the
    conversation's `workspace_id` as the ordinary NON-security narrowing filter
    (distinct from the JWT, which is the trust boundary). The token is minted per
    call and never cached — call this once per turn.

    `config` supplies the validated escalation knobs (US-050) the endpoint built
    once at startup; `match_threshold` is the existing retrieval similarity
    threshold the gate reuses (`retrieval.get_similarity_threshold()`).

    Returns the pipeline's `DeflectionResult`. Only `customer_message` is ever
    safe to surface to the customer; the bot token appears in NONE of the returned
    fields and is not logged here.
    """
    # Mint a fresh token for THIS turn. The "no caching across turns" guarantee
    # is structural: the token is held only in this local and the headers dict
    # below — there is NO module- or instance-level cache anywhere — so the next
    # turn necessarily calls mint_token again. Both locals fall out of scope when
    # the pipeline call returns; we deliberately do NOT pretend to "scrub" the
    # secret from memory (rebinding the local would not touch the copy already
    # inside the headers, so it would be theatre, not hygiene).
    bot_token = mint_token(bot_user_id, BOT_TOKEN_TTL_SECONDS)
    supabase_headers = build_bot_supabase_headers(bot_token, anon_key)
    return await run_deflection_pipeline(
        embedder_client=embedder_client,
        answerer_client=answerer_client,
        judge_client=judge_client,
        http=http,
        supabase_url=supabase_url,
        supabase_headers=supabase_headers,
        message=message,
        tau_sim=config.tau_sim,
        n_min=config.n_min,
        match_threshold=match_threshold,
        faithfulness_cutoff=config.faithfulness_cutoff,
        top_k=top_k,
        answerer_model=answerer_model,
        judge_model=judge_model,
        workspace_id=workspace_id,
    )
