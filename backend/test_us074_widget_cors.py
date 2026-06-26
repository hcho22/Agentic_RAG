"""US-074: the public widget surface has its OWN CORS posture (ADR-0008).

The authenticated app (`/api/*`, `/healthz`, everything that is NOT a `/widget/*`
route) keeps its historical posture: a static allowlist = `FRONTEND_ORIGINS`. The
PUBLIC widget surface (`/widget/*`) trusts ONLY the origins registered on an active
(not-revoked) widget key — the union of every `widget_keys.allowed_origins` — and
NEVER `FRONTEND_ORIGINS`. The two origin sets are independent: an app origin is
rejected at `/widget/*`, and a widget origin is rejected at `/api/*`.

Layers (in the style of `test_us073_widget_key_origin.py`):

  * a UNIT layer (security-critical, runs whenever the app imports — it needs no
    DB / no network): the sync origin matcher `_WidgetOriginSnapshot.allows` and the
    path partition `_is_widget_path`. `allows` mirrors `widget_keys.is_origin_allowed`'s
    fail-closed rules at the CORS layer (missing/blank Origin refused even under the
    dev-only `"*"`; exact, un-normalized membership otherwise). The partition must be
    COMPLEMENTARY so the two CORS postures never both touch one request.

  * a LOADER layer: `_load_active_widget_origins` FAILS CLOSED — it returns
    `(empty, False)` when support is unconfigured (no service-role key) so the public
    CORS surface denies every origin rather than fail-open; and when it does read, it
    hoists the dev-only `"*"` into the wildcard flag and drops blank entries.

  * an INTEGRATION / SECURITY layer (skips cleanly if the app cannot be imported),
    encoding the PRD US-074 "Validation Test" end-to-end through real preflights via a
    FastAPI TestClient with the widget-origin loader mocked (the dynamic allowlist
    lives in the app, not the DB):

      Setup: FRONTEND_ORIGINS = [<app origin>]; widget key registers <client origin>.
      1. Preflight `/widget/keys/resolve` with the client origin  -> allowed (ACAO echoed).
      2. Preflight the same with the APP origin (not registered)   -> rejected (no ACAO).
      3. Preflight `/api/chat` with the client (widget) origin     -> rejected (no ACAO).
      Plus: `/api/*` with the app origin is STILL allowed (the authenticated posture is
      unchanged), a simple (non-preflight) widget request reflects ACAO on the actual
      response, the dev-only `"*"` admits any present origin but still refuses an
      originless request, and a cold/empty snapshot denies all (fail-closed).

    Failure indicator (the fail-OPEN bug this test exists to catch): the public widget
    surface trusts `FRONTEND_ORIGINS`, or `/api/*` trusts a widget origin, or a
    disallowed origin still gets an `Access-Control-Allow-Origin`.

CORS is a BROWSER-side control and defense-in-depth here, NOT the hard boundary — the
authoritative per-key origin gate (US-073) + not-revoked gate (US-072) + rate limit
(US-076) run in the endpoints under the real `public_key`.

Run:
    python -m backend.test_us074_widget_cors

Everything mocks the DB loader; no Supabase round-trip, no OpenAI.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The app reads FRONTEND_ORIGIN + SUPABASE_* / a provider key at import time. Supply
# well-known local defaults so the import succeeds without a real deployment; only
# set what is missing so a real environment is never clobbered. FRONTEND_ORIGIN is
# the authenticated-app origin the validation test pins as "must NOT be trusted by
# the widget surface"; the integration layer reads it back from main.FRONTEND_ORIGINS
# rather than hard-coding it, so the test is correct under any default.
os.environ.setdefault("FRONTEND_ORIGIN", "https://app.kit")
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

WIDGET_ORIGIN = "https://client.example"  # a buyer's page — registered on a key
OTHER_ORIGIN = "https://evil.example"     # never registered anywhere


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


# --------------------------------------------------------------------------- #
# Unit layer — the sync matcher + the path partition. SECURITY-CRITICAL, no I/O.
# --------------------------------------------------------------------------- #
def _run_unit(main) -> int:
    checks = 0

    # The path partition must be COMPLEMENTARY: a request is a PUBLIC widget request
    # iff it is NOT an authenticated-app request. The two CORS layers key off this, so
    # a gap (a path owned by neither) or an overlap (owned by both → double ACAO) is a
    # bug.
    assert main._is_widget_public_path("/widget/keys/resolve") is True
    assert main._is_widget_public_path("/widget/conversations/resume") is True
    assert main._is_widget_public_path("/api/chat") is False
    assert main._is_widget_public_path("/healthz") is False
    # A path that merely contains "widget" elsewhere is NOT a widget path (prefix-anchored).
    assert main._is_widget_public_path("/api/support/widget-keys") is False
    # An AUTHENTICATED route under /widget/* (US-082 agent-reply, called by an agent on
    # an app origin) is NOT public — it must fall to the authenticated CORS posture, or
    # the operator dashboard's own app origin would be CORS-blocked.
    assert main._is_widget_public_path("/widget/conversations/abc-123/agent-reply") is False
    checks += 1
    print("  unit: _is_widget_public_path partitions anonymous /widget/* vs the authenticated app (incl agent-reply exception)")

    snap = main._WidgetOriginSnapshot(ttl_seconds=30.0)

    # A registered origin is admitted; an unregistered one is refused (exact membership).
    snap._origins = frozenset({WIDGET_ORIGIN})
    snap._wildcard = False
    assert snap.allows(WIDGET_ORIGIN) is True
    assert snap.allows(OTHER_ORIGIN) is False
    checks += 1
    print("  unit: snapshot admits a registered origin; refuses an unregistered one")

    # Fail-closed on a missing / blank / whitespace-only Origin — the cross-origin
    # widget always emits one, so its absence means refuse, not allow.
    assert snap.allows(None) is False
    assert snap.allows("") is False
    assert snap.allows("   ") is False
    checks += 1
    print("  unit: missing/blank/whitespace Origin refused (fail-closed)")

    # Exact, un-normalized comparison — trailing slash / case / port mismatch all fail
    # CLOSED, mirroring widget_keys.is_origin_allowed exactly.
    assert snap.allows(WIDGET_ORIGIN + "/") is False
    assert snap.allows("https://Client.Example") is False
    assert snap.allows(WIDGET_ORIGIN + ":8443") is False
    checks += 1
    print("  unit: exact comparison — trailing-slash / case / port mismatch fail-closed")

    # Dev-only `"*"` wildcard: any PRESENT origin is admitted, but an originless
    # request is STILL refused (the wildcard never rescues a missing Origin).
    snap._origins = frozenset()
    snap._wildcard = True
    assert snap.allows(OTHER_ORIGIN) is True
    assert snap.allows("https://anything.test") is True
    assert snap.allows(None) is False, "wildcard must NOT admit an originless request"
    assert snap.allows("") is False
    checks += 1
    print("  unit: '*' wildcard admits any present origin but still refuses originless (dev-only)")

    # A cold / empty snapshot (no keys, or a failed load) denies everything — the
    # fail-closed default the public CORS surface must hold.
    cold = main._WidgetOriginSnapshot(ttl_seconds=30.0)
    assert cold.allows(WIDGET_ORIGIN) is False
    assert cold.allows(OTHER_ORIGIN) is False
    checks += 1
    print("  unit: cold/empty snapshot denies all (fail-closed default)")

    return checks


# --------------------------------------------------------------------------- #
# Loader layer — `_load_active_widget_origins` fails closed + parses correctly.
# --------------------------------------------------------------------------- #
def _run_loader(main) -> int:
    checks = 0

    # Fail-closed when support is unconfigured: no service-role key → no widget surface
    # to admit any origin for → (empty, False), and crucially WITHOUT any network I/O.
    orig_headers = main._service_role_headers
    main._service_role_headers = lambda: None  # type: ignore[assignment]
    try:
        origins, wildcard = asyncio.run(main._load_active_widget_origins())
    finally:
        main._service_role_headers = orig_headers  # type: ignore[assignment]
    assert origins == frozenset() and wildcard is False, (
        "unconfigured support must deny all widget origins (fail-closed)"
    )
    checks += 1
    print("  loader: service-role unset -> (empty, False) with no I/O (fail-closed)")

    # When it DOES read: union across active keys, hoist the dev-only "*" into the
    # wildcard flag (never store it as a matchable origin), drop blank entries.
    class _FakeResp:
        def json(self) -> list[dict]:
            return [
                {"allowed_origins": [WIDGET_ORIGIN, "  "]},   # blank dropped
                {"allowed_origins": ["https://b.example"]},
                {"allowed_origins": ["*", ""]},               # "*" hoisted, "" dropped
                {"allowed_origins": None},                     # tolerated
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

        async def get(self, *a: object, **k: object) -> _FakeResp:
            return _FakeResp()

    orig_client = main.httpx.AsyncClient
    main._service_role_headers = lambda: {"apikey": "k", "Authorization": "Bearer k"}  # type: ignore[assignment]
    main.httpx.AsyncClient = _FakeClient  # type: ignore[assignment,misc]
    try:
        origins, wildcard = asyncio.run(main._load_active_widget_origins())
    finally:
        main.httpx.AsyncClient = orig_client  # type: ignore[assignment,misc]
        main._service_role_headers = orig_headers  # type: ignore[assignment]
    assert origins == frozenset({WIDGET_ORIGIN, "https://b.example"}), (
        f"union must drop blanks and the '*' sentinel, got {origins}"
    )
    assert wildcard is True, "an active key carrying '*' must set the wildcard flag"
    assert "*" not in origins and "" not in origins and "  " not in origins
    checks += 1
    print("  loader: union across keys — '*' hoisted to wildcard, blanks dropped")

    return checks


# --------------------------------------------------------------------------- #
# Integration / security layer — the PRD validation test through real preflights.
# --------------------------------------------------------------------------- #
def _run_integration(main) -> int:
    from fastapi.testclient import TestClient  # noqa: E402

    app_origins = main.FRONTEND_ORIGINS
    assert app_origins, "the authenticated app must have at least one FRONTEND_ORIGIN"
    APP_ORIGIN = app_origins[0]
    # The validation test hinges on the widget origin NOT being an app origin; if a
    # real env happened to register it, the partition can't be demonstrated.
    if WIDGET_ORIGIN in app_origins:  # pragma: no cover - env-dependent
        print(f"SKIP integration: widget origin {WIDGET_ORIGIN} is also a FRONTEND_ORIGIN")
        return 0

    # Mock the dynamic widget-origin loader: the only registered origin is the buyer's
    # client origin (one key allows it). No DB, no network.
    async def _fake_load_client() -> tuple[frozenset[str], bool]:
        return frozenset({WIDGET_ORIGIN}), False

    async def _fake_resolve(http: object, public_key: str) -> dict | None:
        # Used only by the simple-response sub-check; resolution itself is US-072/073's
        # concern, so a None (404) keeps this focused on the CORS header behavior.
        return None

    orig_load = main._load_active_widget_origins
    orig_resolve = main._resolve_widget_key
    main._load_active_widget_origins = _fake_load_client  # type: ignore[assignment]
    main._resolve_widget_key = _fake_resolve  # type: ignore[assignment]
    main._WIDGET_ORIGIN_SNAPSHOT.invalidate()
    total = 0
    try:
        client = TestClient(main.app)

        def preflight(url: str, origin: str | None) -> tuple[int, str | None]:
            headers = {"Access-Control-Request-Method": "POST"}
            if origin is not None:
                headers["Origin"] = origin
            r = client.options(url, headers=headers)
            return r.status_code, r.headers.get("access-control-allow-origin")

        # Step 1: preflight a widget endpoint with the registered client origin ->
        # allowed, and the reflected ACAO is the SPECIFIC origin (never "*").
        _, acao = preflight("/widget/keys/resolve", WIDGET_ORIGIN)
        assert acao == WIDGET_ORIGIN, f"widget+registered origin must echo ACAO, got {acao!r}"
        total += 1
        print("  step 1: preflight /widget with the registered origin -> ACAO echoed")

        # Step 2: preflight the SAME widget endpoint with the APP origin (a trusted
        # /api origin, but NOT registered on any widget key) -> rejected, no ACAO. This
        # is the "public surface must not inherit FRONTEND_ORIGINS" property.
        _, acao = preflight("/widget/keys/resolve", APP_ORIGIN)
        assert acao != APP_ORIGIN, (
            f"the widget surface must NOT trust the app origin {APP_ORIGIN!r} (got ACAO {acao!r})"
        )
        assert acao is None
        total += 1
        print("  step 2: preflight /widget with the APP origin -> rejected (no ACAO)")

        # Step 3: preflight `/api/chat` with the widget (client) origin -> rejected. The
        # authenticated surface only trusts FRONTEND_ORIGINS, never a widget origin.
        _, acao = preflight("/api/chat", WIDGET_ORIGIN)
        assert acao != WIDGET_ORIGIN, (
            f"/api/* must NOT trust the widget origin {WIDGET_ORIGIN!r} (got ACAO {acao!r})"
        )
        assert acao is None
        total += 1
        print("  step 3: preflight /api/chat with the widget origin -> rejected (no ACAO)")

        # The authenticated posture is UNCHANGED: `/api/*` still admits its own app
        # origin (no widening, no regression from the single-middleware config).
        code, acao = preflight("/api/chat", APP_ORIGIN)
        assert code == 200 and acao == APP_ORIGIN, (
            f"/api/* must still admit the app origin {APP_ORIGIN!r}, got {code} {acao!r}"
        )
        total += 1
        print("  control: preflight /api/chat with the app origin -> allowed (posture unchanged)")

        # A simple (non-preflight) widget request reflects ACAO on the ACTUAL response
        # for a registered origin, and reflects NOTHING for an unregistered one — even
        # though the body is the same opaque 404 either way.
        r_ok = client.post("/widget/keys/resolve", json={"public_key": "wk_pk_x"}, headers={"Origin": WIDGET_ORIGIN})
        assert r_ok.headers.get("access-control-allow-origin") == WIDGET_ORIGIN, (
            "a simple widget response must reflect ACAO for a registered origin"
        )
        r_no = client.post("/widget/keys/resolve", json={"public_key": "wk_pk_x"}, headers={"Origin": OTHER_ORIGIN})
        assert r_no.headers.get("access-control-allow-origin") is None, (
            "a simple widget response must NOT reflect ACAO for an unregistered origin"
        )
        total += 1
        print("  control: simple widget response reflects ACAO only for a registered origin")

        # Dev-only "*" opt-in: with a wildcard key active, any PRESENT origin is admitted
        # (ACAO still the specific origin, never literal "*"), but an originless request
        # is still refused.
        async def _fake_load_wild() -> tuple[frozenset[str], bool]:
            return frozenset(), True

        main._load_active_widget_origins = _fake_load_wild  # type: ignore[assignment]
        main._WIDGET_ORIGIN_SNAPSHOT.invalidate()
        _, acao = preflight("/widget/keys/resolve", OTHER_ORIGIN)
        assert acao == OTHER_ORIGIN, f"'*' must admit any present origin (echoed), got {acao!r}"
        _, acao = preflight("/widget/keys/resolve", None)
        assert acao is None, "'*' must NOT rescue an originless request"
        total += 1
        print("  control: dev-only '*' admits any present origin, still refuses originless")

        # Fail-closed: an empty snapshot (no active keys / a failed load) denies all.
        async def _fake_load_empty() -> tuple[frozenset[str], bool]:
            return frozenset(), False

        main._load_active_widget_origins = _fake_load_empty  # type: ignore[assignment]
        main._WIDGET_ORIGIN_SNAPSHOT.invalidate()
        _, acao = preflight("/widget/keys/resolve", WIDGET_ORIGIN)
        assert acao is None, "an empty snapshot must deny all widget origins (fail-closed)"
        total += 1
        print("  control: empty snapshot denies all widget origins (fail-closed)")

        print(
            f"OK: US-074 integration passed — {total} preflight/CORS assertions; the public "
            "widget surface trusts ONLY per-key registered origins (never FRONTEND_ORIGINS), "
            "/api/* never trusts a widget origin, and every disallowed origin gets NO ACAO"
        )
        return total
    finally:
        main._load_active_widget_origins = orig_load  # type: ignore[assignment]
        main._resolve_widget_key = orig_resolve  # type: ignore[assignment]
        main._WIDGET_ORIGIN_SNAPSHOT.invalidate()


def _run() -> None:
    main = _import_main()
    if main is None:
        return
    unit = _run_unit(main)
    print(f"  ({unit} unit checks passed)")
    loader = _run_loader(main)
    print(f"  ({loader} loader checks passed)")
    _run_integration(main)


def main_entry() -> None:
    _run()


if __name__ == "__main__":
    main_entry()
