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
  * the pinned sampler (issue #104), and that NO error shape is ever retried —
    including a 400 that names `temperature`, whose remedy is the typed-out
    `JUDGE_TEMPERATURE=none` rather than anything inferred at call time;
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
    AnswerDecision,
    AnswerJudgment,
    answer_gate,
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


def _judge_error(message: str) -> Exception:
    """A judge-call failure, carrying nothing but its message.

    Deliberately status-free: `_judge_parse` treats EVERY exception identically,
    so an HTTP status is not part of the contract and attaching one here would
    imply the gate reads it.
    """
    return Exception(message)


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


def test_every_judge_failure_fails_closed_in_one_call() -> None:
    """No error shape is ever retried: every failure is one call, then escalate.

    `_judge_parse` makes exactly one call with no exception at all - there is no
    error-text inference and no un-pinned retry. A judge deployment that will not
    accept the `temperature` PARAMETER is a CONFIGURATION fact stated once with
    `JUDGE_TEMPERATURE=none`, so the 400 that names the parameter is pinned here as
    just another fail-closed error, exactly like a rate limit or an auth failure.
    The errors below therefore carry only a message - the gate never reads a status.
    """
    saved_temp = os.environ.pop("JUDGE_TEMPERATURE", None)
    try:
        for label, exc in (
            ("other 400", _judge_error("Invalid schema for response_format")),
            ("rate limit", _judge_error("Rate limit reached for requests")),
            ("auth", _judge_error("Incorrect API key provided")),
            (
                "400 naming temperature",
                _judge_error(
                    "Unsupported parameter: 'temperature' is not supported with "
                    "this model."
                ),
            ),
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
    finally:
        if saved_temp is not None:
            os.environ["JUDGE_TEMPERATURE"] = saved_temp
    print("ok: every judge failure fails closed in exactly one call, never retried")




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
        test_every_judge_failure_fails_closed_in_one_call,
        test_decision_is_frozen,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} answer-completeness-gate (issue #97) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
