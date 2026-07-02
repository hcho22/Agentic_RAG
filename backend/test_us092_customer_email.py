"""US-092: an OPTIONAL customer email collected AT escalation — never a gate.

The customer may leave an email at the point of escalation ("leave your email and a
human will follow up") so an agent can follow up manually. It is metadata ONLY —
stored on `conversations.customer_email` (US-066), shown to the agent in the US-087
queue — NEVER a retrieval principal, NEVER an auth identity, and NEVER required to
escalate (ADR-0004; v1 sends no automated email — there is no ESP integration). The
`customer_email` column already exists (US-066), so this story adds no migration.

Two layers, the same shape as the other support-surface tests
(`test_us080_escalation_latch.py`, `test_us079_deflection_streaming.py`):

  * a UNIT layer (always runs, no DB / no LLM / no app network):
      - `_normalize_customer_email` returns None for a missing/blank value (so a
        blank email never blocks the handoff — the PRD failure indicator), trims a
        valid one, and raises 400 on a clearly-malformed non-blank value.
      - `_escalate_conversation` folds the OPTIONAL email into the SAME service-role
        PATCH: with no email the body is EXACTLY `{"status": "escalated"}` (the
        US-080 invariant — escalated_at is the US-067 trigger's, never planted), and
        with an email the body additionally carries `customer_email`. Pinned with an
        httpx `MockTransport` that captures the request.

  * an INTEGRATION layer (skips cleanly when the app can't import), encoding the PRD
    US-092 "Validation Test" through the REAL `POST /widget/conversations/escalate`
    via a FastAPI TestClient with `_resume_conversation_by_token` + `_escalate_conversation`
    mocked (no Supabase): conversation C1 escalates with NO email (blank/bodyless —
    stored customer_email is None), C2 escalates WITH an email (stored to the row),
    both reach `status='escalated'`, a malformed email is a 400 (never a silent bad
    store), a missing token is a 401, and NOTHING on the path sends an email (v1 has
    no ESP: the email only ever reaches the `_escalate_conversation` metadata store).

Run:
    python -m backend.test_us092_customer_email
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# The app reads SUPABASE_* / a provider key at import time. Supply local defaults so
# the import succeeds without a real deployment; only set what is missing.
os.environ.setdefault("SUPABASE_URL", "http://127.0.0.1:54321")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test-key")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")

CREATED_AT = "2026-07-02T00:00:00+00:00"
ESCALATED_AT = "2026-07-02T00:00:05+00:00"
WORKSPACE = "ws-uuid-1"
TOKEN = "tok-raw-secret-value"
EMAIL = "ada@example.com"


def _import_main():
    try:
        import main  # noqa: E402

        return main
    except Exception as e:  # pragma: no cover - environment-dependent
        print(f"SKIP: cannot import backend app ({e})")
        return None


# ---------------------------------------------------------------------------
# UNIT LAYER — always runs (no DB / no LLM / no app network).
# ---------------------------------------------------------------------------
def _unit() -> int:
    import httpx

    import main
    from fastapi import HTTPException

    checks = 0

    # ---- _normalize_customer_email: blank → None (never gates), valid trims, bad 400.
    assert main._normalize_customer_email(None) is None, "missing email → None"
    assert main._normalize_customer_email("") is None, "empty email → None"
    assert main._normalize_customer_email("   ") is None, "whitespace-only email → None"
    assert main._normalize_customer_email("  " + EMAIL + "  ") == EMAIL, (
        "a valid email is trimmed and returned verbatim"
    )
    assert main._normalize_customer_email("a+tag@sub.example.co.uk") == "a+tag@sub.example.co.uk", (
        "plus-addressing and subdomains are accepted (permissive local/domain parts)"
    )
    for bad in ("not-an-email", "a@b", "@b.co", "a@.co", "a b@c.co", "a@b c.co", "x" * 300 + "@y.co"):
        try:
            main._normalize_customer_email(bad)
        except HTTPException as e:
            assert e.status_code == 400, f"malformed {bad!r} → 400"
        else:
            raise AssertionError(f"FAILURE: malformed email {bad!r} should raise 400")
    checks += 1
    print("  unit: _normalize_customer_email — blank→None (no gate), valid trims, malformed→400")

    # ---- _escalate_conversation PATCH body: NO email → status-only (US-080 invariant).
    orig_key = main.SUPABASE_SERVICE_ROLE_KEY
    main.SUPABASE_SERVICE_ROLE_KEY = "service-test-key"  # type: ignore[assignment]
    try:
        def _capture() -> dict:
            captured: dict = {}

            def _handler(request: httpx.Request) -> httpx.Response:
                captured["method"] = request.method
                captured["url"] = str(request.url)
                captured["json"] = json.loads(request.content) if request.content else None
                return httpx.Response(
                    200,
                    json=[{
                        "id": "c1", "status": "escalated",
                        "created_at": CREATED_AT, "escalated_at": ESCALATED_AT,
                        "workspace_id": WORKSPACE,
                    }],
                )

            return captured, _handler  # type: ignore[return-value]

        async def _do(customer_email):
            captured, handler = _capture()
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(transport=transport) as http:
                await main._escalate_conversation(http, "c1", customer_email=customer_email)
            return captured

        no_email = asyncio.run(_do(None))
        assert no_email["method"] == "PATCH" and "id=eq.c1" in no_email["url"]
        assert no_email["json"] == {"status": "escalated"}, (
            "FAILURE INDICATOR: with no email the PATCH must be EXACTLY {'status': "
            "'escalated'} — escalated_at stays the US-067 trigger's, and no null "
            f"customer_email is written (got {no_email['json']!r})"
        )
        checks += 1
        print("  unit: _escalate_conversation with NO email → body is exactly {'status': 'escalated'}")

        with_email = asyncio.run(_do(EMAIL))
        assert with_email["json"] == {"status": "escalated", "customer_email": EMAIL}, (
            "with an email the SAME PATCH also carries customer_email (metadata only, "
            f"escalated_at still untouched); got {with_email['json']!r}"
        )
        checks += 1
        print("  unit: _escalate_conversation WITH an email → body adds customer_email (escalated_at untouched)")
    finally:
        main.SUPABASE_SERVICE_ROLE_KEY = orig_key  # type: ignore[assignment]

    return checks


# ---------------------------------------------------------------------------
# INTEGRATION LAYER — the PRD US-092 validation test through the real endpoint.
# ---------------------------------------------------------------------------
def _integration(main) -> int:
    from fastapi.testclient import TestClient

    # Two conversations: C1 escalated without an email, C2 with one.
    state = {
        "conversation": {
            "id": "conv-1", "workspace_id": WORKSPACE, "status": "active",
            "escalated_at": None, "created_at": CREATED_AT,
        },
        # Every (conversation_id, customer_email) the endpoint hands the metadata
        # store. The ONLY place the email may travel on this path — there is no ESP.
        "escalate_calls": [],
    }

    async def fake_resume(http, raw_token, *, slide=True):
        return dict(state["conversation"]) if raw_token == TOKEN else None

    async def fake_escalate(http, conversation_id, *, customer_email=None):
        state["escalate_calls"].append((conversation_id, customer_email))
        row = dict(state["conversation"])
        if row.get("escalated_at") is None:
            row["escalated_at"] = ESCALATED_AT
        row["status"] = "escalated"
        if customer_email:
            row["customer_email"] = customer_email
        return row

    originals = {
        "_resume_conversation_by_token": main._resume_conversation_by_token,
        "_escalate_conversation": main._escalate_conversation,
        "_RATE_LIMITER": main._RATE_LIMITER,
    }
    main._resume_conversation_by_token = fake_resume   # type: ignore[assignment]
    main._escalate_conversation = fake_escalate        # type: ignore[assignment]
    main._RATE_LIMITER = None  # US-076 session limit no-op  # type: ignore[assignment]

    hdr = {main._CONVERSATION_TOKEN_HEADER: TOKEN}
    total = 0
    try:
        client = TestClient(main.app)

        # ===== Step 1: escalate C1 WITHOUT an email (bodyless POST). =====
        r = client.post("/widget/conversations/escalate", headers=hdr)
        assert r.status_code == 200, f"blank-email escalate must succeed, got {r.status_code} {r.text}"
        assert r.json()["conversation"]["status"] == "escalated", "C1 latches escalated"
        assert state["escalate_calls"][-1] == ("conv-1", None), (
            "FAILURE INDICATOR: a blank/omitted email must NOT block escalation and "
            f"must store a null customer_email (store call was {state['escalate_calls'][-1]!r})"
        )
        total += 1
        print("  validation: C1 escalated with NO email — status='escalated', customer_email=None")

        # An explicit empty string in the body is treated the same as omitted.
        state["escalate_calls"].clear()
        r = client.post("/widget/conversations/escalate", headers=hdr, json={"customer_email": "   "})
        assert r.status_code == 200 and state["escalate_calls"][-1] == ("conv-1", None), (
            "a whitespace-only email normalizes to None and still escalates"
        )
        total += 1
        print("  validation: a whitespace-only email still escalates (customer_email=None)")

        # ===== Step 2: escalate C2 WITH an email. =====
        state["escalate_calls"].clear()
        r = client.post("/widget/conversations/escalate", headers=hdr, json={"customer_email": "  " + EMAIL + "  "})
        assert r.status_code == 200, f"email escalate must succeed, got {r.status_code} {r.text}"
        assert r.json()["conversation"]["status"] == "escalated", "C2 latches escalated"
        assert state["escalate_calls"] == [("conv-1", EMAIL)], (
            "the trimmed email reaches the metadata store exactly once (and NOWHERE "
            f"else — v1 has no ESP); store calls were {state['escalate_calls']!r}"
        )
        total += 1
        print("  validation: C2 escalated WITH an email — trimmed customer_email stored (no auto-send)")

        # ===== A clearly-malformed email is a 400 (never a silent bad store). =====
        state["escalate_calls"].clear()
        r = client.post("/widget/conversations/escalate", headers=hdr, json={"customer_email": "not-an-email"})
        assert r.status_code == 400, f"a malformed email → 400, got {r.status_code}"
        assert state["escalate_calls"] == [], "a malformed email never reaches the store"
        total += 1
        print("  validation: a malformed email → 400 and never stored")

        # ===== A missing token is a 401 (unchanged auth), regardless of an email. =====
        r = client.post("/widget/conversations/escalate", json={"customer_email": EMAIL})
        assert r.status_code == 401, f"no token → 401, got {r.status_code}"
        total += 1
        print("  validation: escalate without a conversation token → 401 (auth unchanged)")

        # ===== "No outbound email in either case": the email only ever reaches the
        #       metadata store. There is no ESP/send-email seam on the widget path. =====
        import inspect
        escalate_src = inspect.getsource(main.widget_conversation_escalate)
        for esp in ("smtp", "sendgrid", "resend", "mailgun", "send_email", "sendmail"):
            assert esp not in escalate_src.lower(), (
                f"FAILURE INDICATOR: the escalate path must not send email (found {esp!r}); "
                "v1 collects customer_email as metadata for manual follow-up only"
            )
        total += 1
        print("  validation: no ESP/send-email seam on the escalate path (metadata-only, manual follow-up)")

    finally:
        for name, fn in originals.items():
            setattr(main, name, fn)

    return total


def _run() -> None:
    unit = _unit()
    print(f"OK: US-092 unit passed — {unit} checks (email normalization + escalate PATCH body).")

    main = _import_main()
    if main is None:
        print("PARTIAL: US-092 unit layer passed; integration skipped (app import unavailable).")
        return

    total = _integration(main)
    print(
        f"OK: US-092 integration passed — {total} scenarios; an optional email is "
        "collected AT escalation (blank still escalates, a value is stored as metadata "
        "for manual follow-up), a malformed value is 400, and nothing on the path "
        "sends an email (v1 has no ESP)."
    )


if __name__ == "__main__":
    _run()
