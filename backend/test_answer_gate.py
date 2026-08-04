"""Issue #97 validation test: the one-call runtime answer-completeness gate.

Exercises `escalation.answer_gate` with a fake judge client that records how many
structured-output calls it receives — no real LLM, no network, no secrets — so it
runs anywhere, exactly like `test_faithfulness_gate.py`. The fake mirrors the
OpenAI SDK shape the gate reads (`completion.choices[0].message.parsed` /
`.refusal`).

The gate is the missing runtime operand: the faithfulness gate proves the draft
doesn't LIE, this gate proves it ANSWERS. A grounded non-answer ("I don't have
that information about X") is trivially faithful, so without this gate it
auto-sends and is counted as a deflection though it answered nothing.

Covers:
  * a real answer -> answers=True, passes;
  * a non-answer / deferral -> fails (escalate);
  * EXACTLY ONE judge call per evaluation;
  * a forced judge exception -> non-answer (fail-closed, escalate);
  * a judge model that REJECTS `temperature` -> retried once WITHOUT it, yielding a
    real verdict rather than a fail-closed escalate, then remembered;
  * the cutoff `>=` boundary, refusal / empty-choices / missing-payload
    fail-closed paths, score clamping to [0,1], and that the QUESTION + DRAFT (but
    NOT the chunks) reach the judge — grounding is the other gate's job.

Run:
    python -m backend.test_answer_gate
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from typing import Any, Callable, cast

from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from escalation import (  # noqa: E402
    _TEMPERATURE_REJECTING_MODELS,
    AnswerDecision,
    AnswerJudgment,
    answer_gate,
    get_judge_model,
)

CUTOFF = 0.5
QUESTION = "What is the warranty period on a refurbished unit?"


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# --- fake judge client ----------------------------------------------------


class _FakeCompletions:
    """Records call count + the args of each `parse`, and delegates the response
    (or exception) to a per-test `behavior` callable."""

    def __init__(self, behavior: Callable[[], Any]) -> None:
        self._behavior = behavior
        self.calls = 0
        self.model_used: str | None = None
        self.messages_used: list[dict[str, str]] | None = None
        self.response_format_used: Any = None
        self.extra_kwargs: dict[str, Any] = {}
        # One entry per call. The temperature fallback is a TWO-call path whose
        # whole point is that the two calls differ, so the last-write-wins
        # `extra_kwargs` alone cannot pin it.
        self.kwargs_history: list[dict[str, Any]] = []

    async def parse(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: Any,
        **kwargs: Any,
    ) -> Any:
        self.calls += 1
        self.model_used = model
        self.messages_used = messages
        self.response_format_used = response_format
        # Issue #104: the gate pins the sampler, so the kwargs it splats in are
        # part of its contract and get recorded like `model` / `messages`.
        self.extra_kwargs = kwargs
        self.kwargs_history.append(kwargs)
        return self._behavior()  # may raise


class _FakeJudge:
    def __init__(self, behavior: Callable[[], Any]) -> None:
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(behavior))

    @property
    def calls(self) -> int:
        return cast(_FakeCompletions, self.chat.completions).calls


def _client(behavior: Callable[[], Any]) -> tuple[AsyncOpenAI, _FakeJudge]:
    fake = _FakeJudge(behavior)
    return cast(AsyncOpenAI, fake), fake


def _api_error(message: str, status_code: int) -> Exception:
    """An SDK-shaped error carrying an HTTP status, like the real client raises."""
    e = Exception(message)
    e.status_code = status_code  # type: ignore[attr-defined]
    return e


def _raises(e: BaseException) -> Callable[[], Any]:
    """A judge behavior that always raises `e`."""

    def behavior() -> Any:
        raise e

    return behavior


class _RejectsTemperature:
    """A judge model that 400s on any call carrying `temperature`.

    The shape OpenAI reasoning models (o-series, `gpt-5-*`) present. Mirrors the
    helper in `test_faithfulness_gate.py`: the two gates share `_judge_parse`, so
    the fallback has to be pinned on BOTH of them, not just the one that is easier
    to reach.
    """

    def __init__(self, judgment: Callable[[], Any]) -> None:
        self._judgment = judgment
        self._completions: _FakeCompletions | None = None

    def bind(self, fake: _FakeJudge) -> None:
        self._completions = cast(_FakeCompletions, fake.chat.completions)

    def __call__(self) -> Any:
        assert self._completions is not None, "bind() the fake judge first"
        if "temperature" in self._completions.extra_kwargs:
            raise _api_error(
                "Unsupported parameter: 'temperature' is not supported with this model.",
                400,
            )
        return self._judgment()


def _completion(
    *, parsed: Any = None, refusal: Any = None, choices: bool = True
) -> Any:
    message = types.SimpleNamespace(parsed=parsed, refusal=refusal)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice] if choices else [])


def _judgment(answers: bool, score: float) -> Callable[[], Any]:
    return lambda: _completion(parsed=AnswerJudgment(answers=answers, score=score))


def _run(
    behavior: Callable[[], Any],
    draft: str,
    *,
    question: str = QUESTION,
    cutoff: float = CUTOFF,
) -> tuple[AnswerDecision, _FakeJudge]:
    client, fake = _client(behavior)
    decision = asyncio.run(answer_gate(client, question, draft, cutoff))
    return decision, fake


# --- tests ----------------------------------------------------------------


def test_real_answer_passes() -> None:
    """A draft the judge marks as answering with score >= cutoff passes, in one
    call."""
    d, fake = _run(_judgment(True, 0.9), "Refurbished units carry a 90-day warranty.")
    _check(d.answers is True, f"a real answer must pass, got {d!r}")
    _check(d.addressed is True, f"addressed must surface True, got {d.addressed!r}")
    _check(d.score == 0.9, f"score must surface 0.9, got {d.score!r}")
    _check(d.reason == "answers", f"reason must be 'answers', got {d.reason!r}")
    _check(fake.calls == 1, f"must make EXACTLY ONE judge call, got {fake.calls}")
    print("ok: real answer -> answers=True in exactly one judge call")


def test_non_answer_fails() -> None:
    """The issue-#97 case: a deferral / 'I don't have that information' the judge
    marks as NOT answering fails (escalate), in one call."""
    d, fake = _run(
        _judgment(False, 0.05),
        "I'm sorry, I don't have that information about refurbished units.",
    )
    _check(d.answers is False, f"a non-answer must fail, got {d!r}")
    _check(d.reason == "non_answer: judge_unaddressed", f"reason got {d.reason!r}")
    _check(fake.calls == 1, f"must make exactly one judge call, got {fake.calls}")
    print("ok: grounded non-answer -> answers=False (escalate) in one call")


def test_judge_exception_fails_closed() -> None:
    """A judge exception must NOT default to answers (fail-open). It fails closed
    -> escalate, still one call attempted."""

    def boom() -> Any:
        raise RuntimeError("judge timeout / 503")

    d, fake = _run(boom, "Refurbished units carry a 90-day warranty.")
    _check(d.answers is False, f"judge error must fail CLOSED, got {d!r}")
    _check(d.addressed is False and d.score == 0.0, f"fail-closed fields wrong: {d!r}")
    _check(
        d.reason.startswith("non_answer: judge_error"),
        f"reason must mark the judge error, got {d.reason!r}",
    )
    _check(fake.calls == 1, f"one call attempted, got {fake.calls}")
    print("ok: judge exception -> fail-closed non-answer (never fail-open auto-send)")


def test_cutoff_is_inclusive_and_enforced() -> None:
    """`addressed AND score >= cutoff`: a score exactly at the cutoff passes; an
    addressed draft scoring just below the cutoff still fails."""
    at, _ = _run(_judgment(True, 0.50), "x")
    _check(at.answers is True, f"score == cutoff must pass (>=), got {at!r}")

    below, _ = _run(_judgment(True, 0.49), "x")
    _check(below.answers is False, f"addressed but score < cutoff must fail, got {below!r}")
    _check(
        below.reason == f"non_answer: score {0.49:.4f} < cutoff {0.50:.4f}",
        f"below-cutoff reason got {below.reason!r}",
    )
    print("ok: cutoff is inclusive (>=); addressed-but-low-score still escalates")


def test_refusal_and_malformed_responses_fail_closed() -> None:
    """Refusal, empty choices, and a missing parsed payload each fail closed —
    one call, answers=False."""
    cases = {
        "judge_refusal": lambda: _completion(parsed=None, refusal="I can't help with that"),
        "judge_no_choices": lambda: _completion(choices=False),
        "judge_no_payload": lambda: _completion(parsed=None),
    }
    for tag, behavior in cases.items():
        d, fake = _run(behavior, "x")
        _check(d.answers is False, f"{tag}: must fail closed, got {d!r}")
        _check(d.reason == f"non_answer: {tag}", f"{tag}: reason got {d.reason!r}")
        _check(fake.calls == 1, f"{tag}: one call, got {fake.calls}")
    print("ok: refusal / empty-choices / missing-payload all fail closed in one call")


def test_score_clamped_to_unit_interval() -> None:
    """An out-of-range score from the model is clamped to [0,1] before the cutoff
    compare (no JSON-schema bound, matching the faithfulness gate)."""
    high, _ = _run(_judgment(True, 1.5), "x")
    _check(high.score == 1.0 and high.answers is True, f"1.5 must clamp to 1.0, got {high!r}")
    low, _ = _run(_judgment(True, -0.3), "x")
    _check(low.score == 0.0 and low.answers is False, f"-0.3 must clamp to 0.0, got {low!r}")
    print("ok: judge score clamped to [0,1] before the cutoff comparison")


def test_question_and_draft_reach_the_judge() -> None:
    """The judge receives the QUESTION and the DRAFT (so a 'zero' isn't a
    structurally-blind pass), under the AnswerJudgment schema — and grounding is
    NOT this gate's concern, so no chunk context is threaded here."""
    _, fake = _run(_judgment(True, 0.9), "Refurbished units carry a 90-day warranty.")
    comp = cast(_FakeCompletions, fake.chat.completions)
    messages = comp.messages_used or []
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    _check(QUESTION in user, "the customer question must be in the judge prompt")
    _check("90-day warranty" in user, "the draft must be in the judge prompt")
    _check(
        comp.response_format_used is AnswerJudgment,
        "the gate must request the AnswerJudgment schema (not FaithfulnessJudgment)",
    )
    print("ok: question + draft reach the judge under the AnswerJudgment schema")


def test_judge_sampling_is_pinned_deterministic() -> None:
    """Issue #104: this gate decides send-vs-escalate, so it must not SAMPLE.

    The 2026-08-03 E7 investigation measured this gate returning `answers=True` on
    2 of 5 identical calls, which made a pinned safety verdict partly a coin flip.
    The semantics of `JUDGE_TEMPERATURE` itself are covered once in
    `test_faithfulness_gate.py`; here we pin that THIS gate's call carries it.
    """
    saved = os.environ.get("JUDGE_TEMPERATURE")
    try:
        os.environ.pop("JUDGE_TEMPERATURE", None)
        _, fake = _run(_judgment(True, 0.9), "The fee is $14.95.")
        _check(
            cast(_FakeCompletions, fake.chat.completions).extra_kwargs
            == {"temperature": 0.0},
            "the answer-gate call must pin temperature=0, got "
            f"{cast(_FakeCompletions, fake.chat.completions).extra_kwargs!r}",
        )
    finally:
        if saved is None:
            os.environ.pop("JUDGE_TEMPERATURE", None)
        else:
            os.environ["JUDGE_TEMPERATURE"] = saved
    print("ok: the answer gate's judge call pins temperature=0")


def test_temperature_rejecting_model_falls_back_to_a_real_verdict() -> None:
    """This gate must survive a judge model that REJECTS `temperature`, too.

    Both gates now send the parameter, so both regressed the same working
    configurations (an OpenAI reasoning model was a fine `JUDGE_MODEL` when
    neither gate sent it). The fallback lives in the shared `_judge_parse`, and
    that sharing is exactly why it has to be pinned here as well as on the
    faithfulness gate - a future refactor could re-inline one of them.

    The fallback yields a REAL verdict: never `judge_error`, never fail-closed,
    so it never reaches the one-way conversation latch. It costs two calls the
    first time the rejecting model is seen and one call thereafter.
    """
    saved_models = set(_TEMPERATURE_REJECTING_MODELS)
    _TEMPERATURE_REJECTING_MODELS.clear()
    # Neutralize an ambient JUDGE_TEMPERATURE. `none` is the documented escape
    # hatch, so a developer shell may well export it; left in place, `_judge_parse`
    # takes its no-sampling branch, the fake never sees a temperature kwarg, and
    # this test fails blaming the fallback for the shell's state.
    saved_temp = os.environ.pop("JUDGE_TEMPERATURE", None)
    try:
        behavior = _RejectsTemperature(_judgment(True, 0.9))
        client, fake = _client(behavior)
        behavior.bind(fake)
        d = asyncio.run(answer_gate(client, QUESTION, "The fee is $14.95.", CUTOFF))

        _check(d.answers is True, f"the fallback must yield a REAL verdict, got {d!r}")
        _check(
            d.reason == "answers",
            f"the fallback must not be reported as a judge failure, got {d.reason!r}",
        )
        comp = cast(_FakeCompletions, fake.chat.completions)
        _check(fake.calls == 2, f"the fallback path makes exactly two calls, got {fake.calls}")
        _check(
            comp.kwargs_history == [{"temperature": 0.0}, {}],
            f"call 1 must carry the pin and call 2 must omit it, got {comp.kwargs_history!r}",
        )
        _check(
            get_judge_model() in _TEMPERATURE_REJECTING_MODELS,
            "the rejecting model must be remembered so later calls skip the probe",
        )

        behavior2 = _RejectsTemperature(_judgment(True, 0.9))
        client2, fake2 = _client(behavior2)
        behavior2.bind(fake2)
        d2 = asyncio.run(answer_gate(client2, QUESTION, "The fee is $14.95.", CUTOFF))
        _check(d2.answers is True, f"the remembered path must still answer, got {d2!r}")
        _check(fake2.calls == 1, f"a remembered rejection costs ONE call, got {fake2.calls}")
        _check(
            cast(_FakeCompletions, fake2.chat.completions).kwargs_history == [{}],
            "a remembered rejection must send no temperature at all",
        )
    finally:
        _TEMPERATURE_REJECTING_MODELS.clear()
        _TEMPERATURE_REJECTING_MODELS.update(saved_models)
        if saved_temp is not None:
            os.environ["JUDGE_TEMPERATURE"] = saved_temp
    print("ok: a temperature-rejecting judge falls back to a real verdict, once")


def test_echoed_payload_400_does_not_unpin_the_gate() -> None:
    """A 400 about ANOTHER parameter must not un-pin the gate just for saying
    "temperature" somewhere.

    Gateways and proxies (LiteLLM, some Azure front-ends) echo the request payload
    into their error text, and that payload carries `"temperature": 0.0`. An
    unanchored check reads the marker attached to `response_format` as a
    temperature rejection and switches off the determinism pin permanently, for
    the rest of the process, over an unrelated failure. The marker has to be
    anchored to the temperature token, and the structured `param` - which names
    the real culprit - is authoritative when present.
    """
    saved_models = set(_TEMPERATURE_REJECTING_MODELS)
    _TEMPERATURE_REJECTING_MODELS.clear()
    saved_temp = os.environ.pop("JUDGE_TEMPERATURE", None)
    try:
        echoed = _api_error(
            "Unsupported parameter: 'response_format' is not supported with this "
            'model. Request body: {"model": "o4-mini", "temperature": 0.0}',
            400,
        )
        d, fake = _run(_raises(echoed), "The fee is $14.95.")
        _check(
            d.answers is False and d.reason.startswith("non_answer: judge_error"),
            f"an unrelated 400 must still fail closed, got {d!r}",
        )
        _check(fake.calls == 1, f"it must NOT be retried, got {fake.calls} calls")
        _check(
            get_judge_model() not in _TEMPERATURE_REJECTING_MODELS,
            "an unrelated 400 must NEVER un-pin the gate for this model",
        )

        # Same shape, but the provider names the offending parameter in structured
        # form. That is authoritative and must decide it outright.
        structured = _api_error("Unsupported parameter: temperature is bad", 400)
        structured.body = {"error": {"param": "response_format"}}  # type: ignore[attr-defined]
        d2, fake2 = _run(_raises(structured), "The fee is $14.95.")
        _check(
            d2.answers is False and fake2.calls == 1,
            f"a structured param naming another argument must not retry, got {d2!r}",
        )
        _check(
            get_judge_model() not in _TEMPERATURE_REJECTING_MODELS,
            "a structured param naming another argument must not un-pin the gate",
        )
    finally:
        _TEMPERATURE_REJECTING_MODELS.clear()
        _TEMPERATURE_REJECTING_MODELS.update(saved_models)
        if saved_temp is not None:
            os.environ["JUDGE_TEMPERATURE"] = saved_temp
    print("ok: an echoed-payload 400 fails closed and leaves the pin ON")


def test_failed_retry_does_not_record_the_model() -> None:
    """If the un-pinned retry ALSO fails, temperature was never the problem.

    The retry is the only thing that confirms the diagnosis, so the model must not
    be recorded until it succeeds - otherwise one misread error un-pins the gate
    for the rest of the process. A misdiagnosis has to cost one wasted call, not
    the determinism property. The caller must also see the ORIGINAL failure, not
    the second-order one from the retry.
    """
    saved_models = set(_TEMPERATURE_REJECTING_MODELS)
    _TEMPERATURE_REJECTING_MODELS.clear()
    saved_temp = os.environ.pop("JUDGE_TEMPERATURE", None)
    try:
        original = _api_error(
            "Unsupported parameter: 'temperature' is not supported with this model.", 400
        )

        def always_fails() -> Any:
            raise original

        d, fake = _run(always_fails, "The fee is $14.95.")
        _check(
            d.answers is False and d.reason.startswith("non_answer: judge_error"),
            f"a retry that also fails must fail closed, got {d!r}",
        )
        _check(fake.calls == 2, f"one pinned attempt plus one retry, got {fake.calls}")
        _check(
            get_judge_model() not in _TEMPERATURE_REJECTING_MODELS,
            "a retry that FAILED must not leave the gate permanently un-pinned",
        )
    finally:
        _TEMPERATURE_REJECTING_MODELS.clear()
        _TEMPERATURE_REJECTING_MODELS.update(saved_models)
        if saved_temp is not None:
            os.environ["JUDGE_TEMPERATURE"] = saved_temp
    print("ok: a failed retry leaves the pin ON and surfaces the original error")


def test_unrelated_bad_request_still_fails_closed() -> None:
    """Only a 400 that names `temperature` is retried; everything else fails closed.

    A rate limit or an auth failure is a DEGRADED judge. Retrying it un-pinned
    would both hide it and return a safety gate to sampling, so the narrowness of
    the fallback is the safety property here, not an implementation detail.
    """
    saved_models = set(_TEMPERATURE_REJECTING_MODELS)
    _TEMPERATURE_REJECTING_MODELS.clear()
    # Neutralize an ambient JUDGE_TEMPERATURE. `none` is the documented escape
    # hatch, so a developer shell may well export it; left in place, `_judge_parse`
    # takes its no-sampling branch, the fake never sees a temperature kwarg, and
    # this test fails blaming the fallback for the shell's state.
    saved_temp = os.environ.pop("JUDGE_TEMPERATURE", None)
    try:
        for label, exc in (
            ("other 400", _api_error("Invalid schema for response_format", 400)),
            ("rate limit", _api_error("Rate limit reached for requests", 429)),
            ("auth", _api_error("Incorrect API key provided", 401)),
            ("429 naming temperature", _api_error("temperature is not supported", 429)),
        ):
            def boom(e: Exception = exc) -> Any:
                raise e

            d, fake = _run(boom, "The fee is $14.95.")
            _check(d.answers is False, f"{label}: must fail CLOSED, got {d!r}")
            _check(
                d.reason == "non_answer: judge_error",
                f"{label}: must stay a judge_error, got {d.reason!r}",
            )
            _check(fake.calls == 1, f"{label}: must not retry, got {fake.calls} calls")
        _check(
            not _TEMPERATURE_REJECTING_MODELS,
            "an unrelated failure must never mark the model as rejecting the parameter",
        )
    finally:
        _TEMPERATURE_REJECTING_MODELS.clear()
        _TEMPERATURE_REJECTING_MODELS.update(saved_models)
        if saved_temp is not None:
            os.environ["JUDGE_TEMPERATURE"] = saved_temp
    print("ok: every non-temperature failure still fails closed in one call")


def test_decision_is_frozen() -> None:
    d = AnswerDecision(answers=True, addressed=True, score=0.9, reason="answers")
    try:
        d.answers = False  # type: ignore[misc]
    except ValueError:
        print("ok: AnswerDecision is frozen (immutable)")
        return
    raise AssertionError("AnswerDecision must be frozen")


def main() -> int:
    tests = [
        test_real_answer_passes,
        test_non_answer_fails,
        test_judge_exception_fails_closed,
        test_cutoff_is_inclusive_and_enforced,
        test_refusal_and_malformed_responses_fail_closed,
        test_score_clamped_to_unit_interval,
        test_question_and_draft_reach_the_judge,
        test_judge_sampling_is_pinned_deterministic,
        test_temperature_rejecting_model_falls_back_to_a_real_verdict,
        test_echoed_payload_400_does_not_unpin_the_gate,
        test_failed_retry_does_not_record_the_model,
        test_unrelated_bad_request_still_fails_closed,
        test_decision_is_frozen,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} answer-completeness-gate (issue #97) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
