"""US-073: per-key registered-origin allowlist, fail-closed.

Two layers, in the style of `test_us072_widget_keys.py`:

  * a UNIT layer (always runs, no DB / no app import): the pure origin-matching
    helper `widget_keys.is_origin_allowed`. This is the SECURITY-CRITICAL core —
    it encodes the fail-closed rules (empty/null allowlist => inactive; missing/
    blank Origin => refuse; `"*"` dev-only wildcard => allow any present origin;
    otherwise exact membership). Every ambiguous case must fail CLOSED.

  * an INTEGRATION / SECURITY layer (skips cleanly when the FastAPI app cannot be
    imported), encoding the PRD US-073 "Validation Test" end-to-end through the
    real `POST /widget/keys/resolve` endpoint via a FastAPI TestClient with the
    US-072 not-revoked resolve gate mocked (the origin gate lives in the ENDPOINT,
    not the DB, so a raw PostgREST round-trip can't exercise it — the TestClient
    drives the actual Origin-header read + `is_origin_allowed` wiring + opaque-404
    mapping):

      Setup: key A with allowed_origins=['https://client.example']; key B with an
      EMPTY allowlist.
      1. Resolve A with `Origin: https://client.example`  -> 200 {"active": true}.
      2. Resolve A with `Origin: https://evil.example`    -> 404 (unlisted origin).
      3. Resolve B (empty allowlist) with any/with origin  -> 404 (inactive).
      Plus: an originless request to A -> 404 (fail-closed), an unknown key -> 404,
      a wildcard key -> allowed for any present origin but still refused originless.
      Every refusal is the SAME opaque body ("unknown or inactive widget key") so
      nothing leaks whether the key exists or which origins it allows.

    Failure indicator (a fail-OPEN bug a test MUST catch): an originless key
    resolves as active, or an unlisted origin resolves successfully.

  * an ISSUANCE-GUARD layer (issue #36, US-073 follow-up; skips cleanly when the
    app cannot be imported): the mirror of the resolution check, on the WRITE
    side. `POST /api/support/widget-keys` rejects an empty/blank `allowed_origins`
    with a hard 400 BEFORE generating a key or touching the DB, so an admin can
    never mint a key that US-073 would render silently inactive. Driven through
    the real endpoint via TestClient with the admin auth dependency overridden;
    the positive control stubs the outbound INSERT to prove a valid allowlist
    still issues. This is defense-in-depth UX, NOT a security boundary — the
    resolution gate already stops an originless key from working.

This is defense-in-depth, NOT a hard control — the public_key is non-secret and
`Origin` is forgeable off-browser; the hard abuse controls are the rate limit +
circuit breaker (US-076/077) and the leaked-key blast radius is the already-public
KB.

Run:
    python -m backend.test_us073_widget_key_origin

The unit layer needs nothing. The integration + issuance-guard layers need only an
importable backend (they mock the DB resolve/insert; no Supabase round-trip). No
OpenAI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from widget_keys import (  # noqa: E402
    WILDCARD_ORIGIN,
    generate_public_key,
    has_registered_origin,
    is_origin_allowed,
)

LISTED = "https://client.example"
OTHER = "https://evil.example"


# --------------------------------------------------------------------------- #
# Unit layer — the pure origin matcher, always runs. SECURITY-CRITICAL.
# --------------------------------------------------------------------------- #
def _run_unit() -> int:
    checks = 0

    # A listed origin is allowed; an unlisted one is refused. Exact membership.
    assert is_origin_allowed(LISTED, [LISTED]) is True
    assert is_origin_allowed(LISTED, ["https://a.example", LISTED, "https://b.example"]) is True
    assert is_origin_allowed(OTHER, [LISTED]) is False
    assert is_origin_allowed(OTHER, ["https://a.example", "https://b.example"]) is False
    checks += 1
    print("  unit: listed origin allowed; unlisted origin refused (exact membership)")

    # Fail-closed on an empty OR null allowlist — a key with no registered origin
    # is INACTIVE. THIS is the originless-key fail-open bug the PRD warns about.
    assert is_origin_allowed(LISTED, []) is False, "empty [] allowlist must fail CLOSED (inactive)"
    assert is_origin_allowed(LISTED, None) is False, "null allowlist must fail CLOSED (inactive)"
    # Empty/null allowlist refuses regardless of the origin presented.
    assert is_origin_allowed(None, []) is False
    assert is_origin_allowed("", None) is False
    checks += 1
    print("  unit: empty [] and null allowlist refuse (fail-closed, key inactive)")

    # Fail-closed on a missing / blank / whitespace-only Origin — the cross-origin
    # widget always sends one, so its absence means refuse, not allow.
    assert is_origin_allowed(None, [LISTED]) is False, "missing Origin must fail CLOSED"
    assert is_origin_allowed("", [LISTED]) is False, "blank Origin must fail CLOSED"
    assert is_origin_allowed("   ", [LISTED]) is False, "whitespace-only Origin must fail CLOSED"
    checks += 1
    print("  unit: missing/blank/whitespace Origin refused (fail-closed)")

    # `"*"` is the documented dev-only wildcard: it matches ANY present origin,
    # alone or mixed with explicit entries. (NON-PRODUCTION — see widget_keys.py.)
    assert is_origin_allowed(OTHER, [WILDCARD_ORIGIN]) is True
    assert is_origin_allowed("https://anything.test", ["*"]) is True
    assert is_origin_allowed(OTHER, [LISTED, "*"]) is True, "'*' mixed with entries still allows"
    assert is_origin_allowed(LISTED, ["*", "https://other.example"]) is True
    checks += 1
    print("  unit: '*' wildcard allows any present origin (alone or mixed) — dev-only")

    # Even under the wildcard, a missing/blank Origin still fails CLOSED: an absent
    # origin is not "any origin", it is no origin to match.
    assert is_origin_allowed(None, [WILDCARD_ORIGIN]) is False, "no Origin refused even under '*'"
    assert is_origin_allowed("", ["*"]) is False
    assert is_origin_allowed("  ", ["*"]) is False
    checks += 1
    print("  unit: '*' does NOT rescue a missing/blank Origin (still fail-closed)")

    # Exact, un-normalized string comparison: a trailing slash, a case difference,
    # or a port mismatch all fail CLOSED (the safe direction for defense-in-depth).
    assert is_origin_allowed("https://client.example/", [LISTED]) is False, "trailing slash != origin"
    assert is_origin_allowed("https://Client.Example", [LISTED]) is False, "case mismatch fails closed"
    assert is_origin_allowed("https://client.example:8443", [LISTED]) is False, "port mismatch fails closed"
    assert is_origin_allowed(LISTED, ["https://client.example:443"]) is False
    checks += 1
    print("  unit: exact comparison — trailing-slash / case / port mismatch all fail-closed")

    # Issue #36 follow-up: the issuance-side guard `has_registered_origin` — the
    # mirror of the empty-allowlist resolution check. It answers "would this key
    # ever resolve?" so issuance can reject a dead-on-arrival key with a 400.
    # Empty/null/blank-only => no usable origin (would be silently inactive).
    assert has_registered_origin([]) is False, "empty allowlist has no usable origin"
    assert has_registered_origin(None) is False, "null allowlist has no usable origin"
    assert has_registered_origin([""]) is False, "blank-only allowlist has no usable origin"
    assert has_registered_origin(["   "]) is False, "whitespace-only allowlist has no usable origin"
    assert has_registered_origin(["", "   "]) is False, "all-blank allowlist has no usable origin"
    # One non-blank entry is enough; the dev-only "*" wildcard counts (it is a
    # non-empty, deliberately permissive allowlist — distinct from empty).
    assert has_registered_origin([LISTED]) is True
    assert has_registered_origin([WILDCARD_ORIGIN]) is True, "'*' wildcard is a usable (non-empty) allowlist"
    assert has_registered_origin(["", LISTED]) is True, "one usable origin among blanks is enough"
    checks += 1
    print("  unit: has_registered_origin — empty/null/blank-only rejected; one usable origin (incl '*') accepted")

    return checks


# --------------------------------------------------------------------------- #
# Integration / security layer — drives the real endpoint via TestClient,
# mocking only the US-072 not-revoked resolve gate (no DB). Skips cleanly if the
# backend cannot be imported.
# --------------------------------------------------------------------------- #
def _run_integration() -> int:
    # The app validates SUPABASE_* / a provider key at import time; supply the
    # well-known local defaults so the import succeeds without a real deployment.
    # Only set what is missing, never clobber a real environment.
    os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

    try:
        import main  # noqa: E402
        from fastapi.testclient import TestClient  # noqa: E402
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP integration: cannot import backend app ({e})")
        return 0

    OPAQUE = "unknown or inactive widget key"

    # Key A: a normal allowlisted key. Key B: empty allowlist (inactive,
    # fail-closed). Key C: dev-only wildcard. The resolve gate is mocked to return
    # each key's allowed_origins exactly as US-072's _resolve_widget_key would;
    # an unknown key resolves to None (revoked/unknown), as in production.
    pk_a = generate_public_key()
    pk_b = generate_public_key()
    pk_c = generate_public_key()
    rows = {
        pk_a: {"id": "a", "workspace_id": "w", "allowed_origins": [LISTED]},
        pk_b: {"id": "b", "workspace_id": "w", "allowed_origins": []},
        pk_c: {"id": "c", "workspace_id": "w", "allowed_origins": [WILDCARD_ORIGIN]},
    }

    async def _fake_resolve(http: object, public_key: str) -> dict | None:
        return rows.get(public_key)

    original = main._resolve_widget_key
    main._resolve_widget_key = _fake_resolve  # type: ignore[assignment]
    total = 0
    try:
        client = TestClient(main.app)

        def resolve(pk: str, origin: str | None) -> tuple[int, dict]:
            headers = {} if origin is None else {"Origin": origin}
            r = client.post("/widget/keys/resolve", json={"public_key": pk}, headers=headers)
            return r.status_code, r.json()

        # Step 1: resolve A with its registered origin -> active.
        code, body = resolve(pk_a, LISTED)
        assert code == 200 and body == {"active": True}, f"A+listed must succeed, got {code} {body}"
        total += 1
        print("  step 1: A with registered Origin -> 200 active")

        # Step 2: resolve A with an unlisted origin -> opaque 404 (the fail-open bug
        # this test exists to catch).
        code, body = resolve(pk_a, OTHER)
        assert code == 404 and body.get("detail") == OPAQUE, (
            f"A+unlisted origin MUST be refused, got {code} {body}"
        )
        total += 1
        print("  step 2: A with unlisted Origin -> 404 (refused)")

        # Step 3: resolve B (empty allowlist) -> opaque 404 (inactive), whether or
        # not an origin is presented. The originless-key fail-open bug, caught.
        code, body = resolve(pk_b, LISTED)
        assert code == 404 and body.get("detail") == OPAQUE, (
            f"B (empty allowlist) MUST be inactive, got {code} {body}"
        )
        code2, _ = resolve(pk_b, None)
        assert code2 == 404, "B with no origin must also be refused"
        total += 1
        print("  step 3: B (empty allowlist) -> 404 (inactive, fail-closed)")

        # Fail-closed: an originless request to a VALID, allowlisted key A is still
        # refused — absence of Origin means refuse, not allow.
        code, body = resolve(pk_a, None)
        assert code == 404 and body.get("detail") == OPAQUE, (
            f"A with NO Origin must be refused (fail-closed), got {code} {body}"
        )
        total += 1
        print("  extra: A with NO Origin header -> 404 (fail-closed)")

        # An unknown / revoked key -> same opaque 404 (not-revoked gate first).
        code, _ = resolve(generate_public_key(), LISTED)
        assert code == 404, "unknown key must 404"
        # A malformed (non-widget-shaped) key -> same opaque 404 (shape guard).
        code, _ = resolve("not-a-key", LISTED)
        assert code == 404, "malformed key must 404"
        total += 1
        print("  extra: unknown + malformed keys -> 404 (same opaque body)")

        # Dev-only wildcard key C: any present origin is admitted, but an originless
        # request is STILL refused (the wildcard never rescues a missing Origin).
        code, body = resolve(pk_c, OTHER)
        assert code == 200 and body == {"active": True}, f"C '*' must admit any origin, got {code} {body}"
        code2, _ = resolve(pk_c, None)
        assert code2 == 404, "wildcard key with NO origin must still be refused (fail-closed)"
        total += 1
        print("  extra: '*' key admits any present Origin but refuses an originless request")

        print(
            f"OK: US-073 integration passed — {total} endpoint assertions; the origin "
            "allowlist is fail-closed (empty=inactive, missing/unlisted origin refused) "
            "with a dev-only '*' opt-in, and every refusal is the same opaque 404"
        )
        return total
    finally:
        main._resolve_widget_key = original  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Issuance guard layer (issue #36, US-073 follow-up) — drives the real
# POST /api/support/widget-keys via TestClient with the admin auth dependency
# overridden. The 400-reject for an empty/blank allowlist fires BEFORE any key is
# generated or the DB is touched, so this needs no Supabase round-trip; the
# positive control stubs the outbound INSERT + bot provisioning to prove a valid
# allowlist still issues (the guard lets it through). Skips cleanly if the
# FastAPI app cannot be imported.
# --------------------------------------------------------------------------- #
def _run_issuance_guard() -> int:
    os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

    try:
        import main  # noqa: E402
        from fastapi.testclient import TestClient  # noqa: E402
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP issuance guard: cannot import backend app ({e})")
        return 0

    # Stand in for the admin's authenticated session (the real get_user does a
    # Supabase round-trip we don't want here) — the guard authorizes nothing, it
    # only validates the request body, so a fake principal is sufficient.
    main.app.dependency_overrides[main.get_user] = lambda: main.AuthedUser(
        id="admin-1", access_token="tok"
    )

    # A fake httpx client so the POSITIVE control never hits the network: it
    # returns a representation row exactly as PostgREST would on a successful
    # INSERT (Prefer: return=representation).
    class _FakeResp:
        status_code = 201

        def json(self) -> list[dict]:
            return [
                {
                    "id": "new-key",
                    "workspace_id": "w",
                    "public_key": generate_public_key(),
                    "allowed_origins": [LISTED],
                }
            ]

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *a: object) -> bool:
            return False

        async def post(self, *a: object, **k: object) -> _FakeResp:
            return _FakeResp()

    async def _noop_bot(http: object, workspace_id: str) -> None:
        return None

    orig_client = main.httpx.AsyncClient
    orig_bot = main._ensure_workspace_bot
    total = 0
    try:
        client = TestClient(main.app)

        def issue(allowed_origins: list[str]) -> tuple[int, dict]:
            r = client.post(
                "/api/support/widget-keys",
                json={"workspace_id": "w", "allowed_origins": allowed_origins},
            )
            return r.status_code, r.json()

        # Empty allowlist -> hard 400, and the body names the cause. This is the
        # whole point of issue #36: a key that US-073 would render silently
        # inactive is refused at creation instead of minted dead.
        code, body = issue([])
        assert code == 400, f"empty allowed_origins must be a 400, got {code} {body}"
        assert "origin" in body.get("detail", "").lower(), (
            f"400 body must explain the empty-origins cause, got {body}"
        )
        total += 1
        print("  issuance: empty allowed_origins -> 400 (rejected before any key/DB write)")

        # A blank/whitespace-only allowlist is just as dead — same 400. (The guard
        # checks for a USABLE origin, not merely a non-empty list.)
        code, body = issue(["   "])
        assert code == 400, f"blank-only allowed_origins must be a 400, got {code} {body}"
        total += 1
        print("  issuance: whitespace-only allowed_origins -> 400 (no usable origin)")

        # Positive control: a real origin is NOT rejected by the guard — issuance
        # proceeds and returns the created key. Stub the outbound INSERT + bot
        # provision so this stays offline.
        main.httpx.AsyncClient = _FakeClient  # type: ignore[assignment,misc]
        main._ensure_workspace_bot = _noop_bot  # type: ignore[assignment]
        code, body = issue([LISTED])
        assert code == 200, f"a valid allowlist must issue (not be guard-rejected), got {code} {body}"
        assert body.get("widget_key", {}).get("public_key", "").startswith("wk_pk_"), (
            f"issued key must carry a public_key, got {body}"
        )
        total += 1
        print("  issuance: a registered origin issues normally (guard lets it through)")

        print(
            f"OK: issue #36 issuance guard passed — {total} endpoint assertions; an "
            "empty/blank allowed_origins is rejected with a 400 at issuance (no "
            "dead-on-arrival key), a valid allowlist still issues"
        )
        return total
    finally:
        main.httpx.AsyncClient = orig_client  # type: ignore[assignment,misc]
        main._ensure_workspace_bot = orig_bot  # type: ignore[assignment]
        main.app.dependency_overrides.pop(main.get_user, None)


async def _run() -> None:
    unit = _run_unit()
    print(f"  ({unit} unit checks passed)")
    _run_integration()
    _run_issuance_guard()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
