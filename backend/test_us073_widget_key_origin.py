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

This is defense-in-depth, NOT a hard control — the public_key is non-secret and
`Origin` is forgeable off-browser; the hard abuse controls are the rate limit +
circuit breaker (US-076/077) and the leaked-key blast radius is the already-public
KB.

Run:
    python -m backend.test_us073_widget_key_origin

The unit layer needs nothing. The integration layer needs only an importable
backend (it mocks the DB resolve gate; no Supabase round-trip). No OpenAI.
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


async def _run() -> None:
    unit = _run_unit()
    print(f"  ({unit} unit checks passed)")
    _run_integration()


def main_entry() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_entry()
