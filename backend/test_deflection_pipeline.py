"""US-049 validation test: the deterministic deflection pipeline orchestrator.

Drives the **real** `escalation.run_deflection_pipeline` end to end: hybrid
retrieval runs through an `httpx.MockTransport` (the Supabase RPCs are stubbed
and call-counted, proving "hybrid, once"), and the embedder / answerer / judge
are call-counting fakes — no real LLM, no DB, no secrets, so it runs anywhere.

Covers the PRD validation test:
  * an off-topic (weak-retrieval) message -> escalate with the generic deferral,
    making ZERO draft calls and ZERO judge calls (the OR short-circuits on its
    cheap left operand);
  * a supported message -> a drafted answer that passed the faithfulness gate,
    with exactly ONE draft call and ONE judge call;
  * the draft is a plain completion with NO `tools` (the agentic loop is never
    entered);
plus strong-but-unfaithful -> escalate, empty-draft -> escalate (no judge call),
and the invariant that an escalation's customer-facing message NEVER leaks the
gate `reason` / scores.

Run:
    python -m backend.test_deflection_pipeline
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any, cast

import httpx
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from escalation import (  # noqa: E402
    GENERIC_DEFERRAL,
    DeflectionResult,
    run_deflection_pipeline,
)

SUPABASE_URL = "http://supabase.test"
TAU, N_MIN, THRESH, CUTOFF = 0.4, 2, 0.3, 0.7


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- RPC rows + transport -------------------------------------------------


def _row(chunk_id: str, similarity: float, *, keyword: bool = False) -> dict[str, Any]:
    return {
        "id": chunk_id,
        "document_id": f"doc-{chunk_id}",
        "chunk_index": 0,
        "content": f"content {chunk_id}",
        "similarity": similarity,
        "filename": f"{chunk_id}.txt",
        "granting_principal_id": None,
        "granting_principal_display": None if keyword else "owner",
    }


def _transport(
    match_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
    counter: dict[str, int],
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/rpc/match_chunks"):
            counter["match"] += 1
            return httpx.Response(200, json=match_rows)
        if path.endswith("/rpc/keyword_search"):
            counter["keyword"] += 1
            return httpx.Response(200, json=keyword_rows)
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    return httpx.MockTransport(handler)


# --- fake clients ---------------------------------------------------------


class _FakeEmbeddings:
    async def create(self, model: str, input: list[str]) -> Any:  # noqa: A002
        data = [types.SimpleNamespace(index=i, embedding=[0.1, 0.2, 0.3]) for i in range(len(input))]
        return types.SimpleNamespace(data=data)


class _FakeEmbedder:
    embeddings = _FakeEmbeddings()


class _AnswererCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        self.last_kwargs = kwargs
        message = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FakeAnswerer:
    def __init__(self, content: str) -> None:
        self.chat = types.SimpleNamespace(completions=_AnswererCompletions(content))

    @property
    def _c(self) -> _AnswererCompletions:
        return cast(_AnswererCompletions, self.chat.completions)


class _JudgeCompletions:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed
        self.calls = 0

    async def parse(self, *, model: str, messages: Any, response_format: Any) -> Any:
        self.calls += 1
        message = types.SimpleNamespace(parsed=self.parsed, refusal=None)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FakeJudge:
    def __init__(self, supported: bool, score: float) -> None:
        # Build the judgment lazily so we don't import the schema at module top.
        from escalation import FaithfulnessJudgment

        parsed = FaithfulnessJudgment(supported=supported, score=score)
        self.chat = types.SimpleNamespace(completions=_JudgeCompletions(parsed))

    @property
    def _c(self) -> _JudgeCompletions:
        return cast(_JudgeCompletions, self.chat.completions)


def _run(
    *,
    match_rows: list[dict[str, Any]],
    keyword_rows: list[dict[str, Any]],
    draft: str = "",
    judge_supported: bool = True,
    judge_score: float = 0.9,
    message: str = "What is your return policy?",
) -> tuple[DeflectionResult, _FakeAnswerer, _FakeJudge, dict[str, int]]:
    counter = {"match": 0, "keyword": 0}
    answerer = _FakeAnswerer(draft)
    judge = _FakeJudge(judge_supported, judge_score)

    async def go() -> DeflectionResult:
        async with httpx.AsyncClient(
            transport=_transport(match_rows, keyword_rows, counter)
        ) as http:
            return await run_deflection_pipeline(
                embedder_client=cast(AsyncOpenAI, _FakeEmbedder()),
                answerer_client=cast(AsyncOpenAI, answerer),
                judge_client=cast(AsyncOpenAI, judge),
                http=http,
                supabase_url=SUPABASE_URL,
                supabase_headers={},
                message=message,
                tau_sim=TAU,
                n_min=N_MIN,
                match_threshold=THRESH,
                faithfulness_cutoff=CUTOFF,
            )

    return asyncio.run(go()), answerer, judge, counter


STRONG = [_row("a", 0.70), _row("b", 0.60)]  # top1 0.70 >= tau, 2 rows >= thresh
WEAK = [_row("a", 0.10), _row("b", 0.09)]  # top1 0.10 < tau
KW = [_row("k", 4.0, keyword=True)]  # keyword side (cosine None)


def _assert_no_reason_leak(result: DeflectionResult) -> None:
    msg = result.customer_message
    _check(msg == GENERIC_DEFERRAL, f"escalation must show the generic deferral verbatim, got {msg!r}")
    _check("cosine" not in msg and "tau" not in msg, "deferral must not leak gate internals")
    _check(result.reason not in msg, "the internal reason must never appear in the customer message")
    _check(not any(ch.isdigit() for ch in msg), "deferral must carry no scores/numbers")


# --- tests ----------------------------------------------------------------


def test_weak_retrieval_short_circuits() -> None:
    """Off-topic message: the retrieval gate returns weak, so the pipeline
    escalates having made ZERO draft and ZERO judge calls — the cheap operand
    decided. Hybrid retrieval still ran exactly once (both RPCs hit once)."""
    result, answerer, judge, counter = _run(
        match_rows=WEAK, keyword_rows=KW, message="Do you sell live alpacas?"
    )
    _check(result.action == "escalated", f"weak retrieval must escalate, got {result.action}")
    _check(result.escalated is True, "escalated property must agree with action")
    _check(answerer._c.calls == 0, f"weak path must make ZERO draft calls, got {answerer._c.calls}")
    _check(judge._c.calls == 0, f"weak path must make ZERO judge calls, got {judge._c.calls}")
    _check(result.faithfulness is None, "no faithfulness decision when short-circuited at retrieval")
    _check(result.retrieval.strong is False, "retrieval decision must be recorded as weak")
    _check(counter == {"match": 1, "keyword": 1}, f"hybrid must retrieve ONCE, got {counter}")
    _assert_no_reason_leak(result)
    print("ok: weak retrieval -> escalate, 0 draft + 0 judge calls (short-circuit), hybrid once")


def test_supported_message_is_answered() -> None:
    """Supported message: strong retrieval -> draft -> faithful -> answer. Exactly
    one draft call and one judge call; the draft is a plain completion with NO
    tools (the agentic loop is never entered)."""
    draft = "Our return window is 30 days from delivery."
    result, answerer, judge, counter = _run(
        match_rows=STRONG, keyword_rows=KW, draft=draft, judge_supported=True, judge_score=0.92
    )
    _check(result.action == "answered", f"supported message must be answered, got {result.action}")
    _check(result.customer_message == draft, f"the drafted answer must be returned, got {result.customer_message!r}")
    _check(answerer._c.calls == 1, f"exactly ONE draft call, got {answerer._c.calls}")
    _check(judge._c.calls == 1, f"exactly ONE judge call, got {judge._c.calls}")
    _check(
        answerer._c.last_kwargs is not None and "tools" not in answerer._c.last_kwargs,
        "the draft must be a plain completion with NO tools (no agentic loop, no escalate() tool)",
    )
    _check(result.faithfulness is not None and result.faithfulness.faithful is True, "must record a faithful decision")
    _check(counter == {"match": 1, "keyword": 1}, f"hybrid must retrieve once, got {counter}")
    print("ok: supported -> answered with 1 draft + 1 judge call; draft has no tools")


def test_strong_but_unfaithful_escalates() -> None:
    """Strong retrieval but the judge rejects the draft -> escalate. The draft and
    judge calls both happened (1 each), but the customer sees only the deferral
    and the faithfulness decision is recorded internally."""
    result, answerer, judge, _ = _run(
        match_rows=STRONG,
        keyword_rows=KW,
        draft="We offer lifetime free returns on everything, no questions asked.",
        judge_supported=False,
        judge_score=0.15,
    )
    _check(result.action == "escalated", f"unfaithful draft must escalate, got {result.action}")
    _check(answerer._c.calls == 1, f"draft was attempted once, got {answerer._c.calls}")
    _check(judge._c.calls == 1, f"judge ran once, got {judge._c.calls}")
    _check(result.faithfulness is not None and result.faithfulness.faithful is False, "must record the unfaithful decision")
    _assert_no_reason_leak(result)
    print("ok: strong-but-unfaithful -> escalate (deferral), faithfulness recorded internally")


def test_empty_draft_escalates_without_judge() -> None:
    """A draft that comes back empty escalates (fail-closed) and never reaches the
    judge — no point grading an empty answer."""
    result, answerer, judge, _ = _run(
        match_rows=STRONG, keyword_rows=KW, draft="   ", judge_supported=True, judge_score=1.0
    )
    _check(result.action == "escalated", f"empty draft must escalate, got {result.action}")
    _check(answerer._c.calls == 1, "draft was attempted once")
    _check(judge._c.calls == 0, f"empty draft must NOT reach the judge, got {judge._c.calls}")
    _check(result.reason == "draft_empty", f"reason got {result.reason!r}")
    _assert_no_reason_leak(result)
    print("ok: empty draft -> escalate without a judge call")


def test_result_is_frozen() -> None:
    result, _, _, _ = _run(match_rows=WEAK, keyword_rows=KW)
    try:
        result.action = "answered"  # type: ignore[misc]
    except ValueError:
        print("ok: DeflectionResult is frozen (immutable)")
        return
    raise AssertionError("DeflectionResult must be frozen")


def main() -> int:
    tests = [
        test_weak_retrieval_short_circuits,
        test_supported_message_is_answered,
        test_strong_but_unfaithful_escalates,
        test_empty_draft_escalates_without_judge,
        test_result_is_frozen,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} deflection-pipeline (US-049) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
