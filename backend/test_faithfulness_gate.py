"""US-048 validation test: the one-call runtime faithfulness gate (ADR-0003).

Exercises `escalation.faithfulness_gate` with a fake judge client that records
how many structured-output calls it receives — no real LLM, no network, no
secrets — so it runs anywhere, like `test_chat_mode_default.py`. The fake mirrors
the OpenAI SDK shape the gate reads (`completion.choices[0].message.parsed` /
`.refusal`), exactly as `metadata.extract_document_metadata` consumes it.

Covers the PRD validation test:
  * a grounded draft -> supported=True, passes (faithful);
  * an ungrounded draft -> fails;
  * EXACTLY ONE judge call per evaluation (no RAGAS-style multi-call decomp);
  * a forced judge exception -> unfaithful (fail-closed, escalate);
plus the cutoff `>=` boundary, refusal / empty-choices / missing-payload
fail-closed paths, score clamping to [0,1], and the JUDGE_MODEL selector.

Run:
    python -m backend.test_faithfulness_gate
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
    DEFAULT_JUDGE_MODEL,
    FaithfulnessDecision,
    FaithfulnessJudgment,
    faithfulness_gate,
    get_judge_model,
    get_judge_temperature,
)
from retrieval import SearchDocumentsResult  # noqa: E402

CUTOFF = 0.7


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _chunk(content: str) -> SearchDocumentsResult:
    return SearchDocumentsResult(
        id="c", document_id="d", chunk_index=0, content=content, similarity=0.9, filename="f.txt"
    )


CHUNKS = [_chunk("The return window is 30 days."), _chunk("Refunds post in 5 business days.")]


# --- fake judge client ----------------------------------------------------


class _FakeCompletions:
    """Records call count + the args of each `parse`, and delegates the response
    (or exception) to a per-test `behavior` callable."""

    def __init__(self, behavior: Callable[[], Any]) -> None:
        self._behavior = behavior
        self.calls = 0
        self.model_used: str | None = None
        self.messages_used: list[dict[str, str]] | None = None
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


def _api_error(message: str, status_code: int) -> Exception:
    """An SDK-shaped error carrying an HTTP status, like the real client raises."""
    e = Exception(message)
    e.status_code = status_code  # type: ignore[attr-defined]
    return e


def _bad_request(message: str) -> Exception:
    return _api_error(message, 400)


def _completion(
    *, parsed: Any = None, refusal: Any = None, choices: bool = True
) -> Any:
    message = types.SimpleNamespace(parsed=parsed, refusal=refusal)
    choice = types.SimpleNamespace(message=message)
    return types.SimpleNamespace(choices=[choice] if choices else [])


def _judgment(supported: bool, score: float) -> Callable[[], Any]:
    return lambda: _completion(parsed=FaithfulnessJudgment(supported=supported, score=score))


def _run(
    behavior: Callable[[], Any], draft: str, cutoff: float = CUTOFF
) -> tuple[FaithfulnessDecision, _FakeJudge]:
    client, fake = _client(behavior)
    decision = asyncio.run(faithfulness_gate(client, draft, CHUNKS, cutoff))
    return decision, fake


# --- tests ----------------------------------------------------------------


def test_grounded_draft_passes() -> None:
    """A draft the judge marks supported with score >= cutoff passes, in exactly
    one call."""
    d, fake = _run(_judgment(True, 0.92), "Returns are accepted for 30 days.")
    _check(d.faithful is True, f"grounded draft must be faithful, got {d!r}")
    _check(d.supported is True, f"supported must surface True, got {d.supported!r}")
    _check(d.score == 0.92, f"score must surface 0.92, got {d.score!r}")
    _check(d.reason == "faithful", f"reason must be 'faithful', got {d.reason!r}")
    _check(fake.calls == 1, f"must make EXACTLY ONE judge call, got {fake.calls}")
    print("ok: grounded draft -> faithful=True in exactly one judge call")


def test_ungrounded_draft_fails() -> None:
    """A draft the judge marks unsupported fails (escalate), in one call."""
    d, fake = _run(_judgment(False, 0.10), "We offer lifetime free returns worldwide.")
    _check(d.faithful is False, f"ungrounded draft must fail, got {d!r}")
    _check(d.reason == "unfaithful: judge_unsupported", f"reason got {d.reason!r}")
    _check(fake.calls == 1, f"must make exactly one judge call, got {fake.calls}")
    print("ok: ungrounded draft -> faithful=False (escalate) in one call")


def test_judge_exception_fails_closed() -> None:
    """The PRD's forced-error case: a judge exception must NOT default to
    faithful (fail-open). It fails closed -> escalate, still one call attempted."""

    def boom() -> Any:
        raise RuntimeError("judge timeout / 503")

    d, fake = _run(boom, "Returns are accepted for 30 days.")
    _check(d.faithful is False, f"judge error must fail CLOSED, got {d!r}")
    _check(d.supported is False and d.score == 0.0, f"fail-closed fields wrong: {d!r}")
    _check(
        d.reason.startswith("unfaithful: judge_error"),
        f"reason must mark the judge error, got {d.reason!r}",
    )
    _check(fake.calls == 1, f"one call attempted, got {fake.calls}")
    print("ok: judge exception -> fail-closed unfaithful (never fail-open auto-send)")


def test_cutoff_is_inclusive_and_enforced() -> None:
    """`supported AND score >= cutoff`: a score exactly at the cutoff passes; a
    supported draft scoring just below the cutoff still fails."""
    at, _ = _run(_judgment(True, 0.70), "x")
    _check(at.faithful is True, f"score == cutoff must pass (>=), got {at!r}")

    below, _ = _run(_judgment(True, 0.69), "x")
    _check(below.faithful is False, f"supported but score < cutoff must fail, got {below!r}")
    _check(
        below.reason == f"unfaithful: score {0.69:.4f} < cutoff {0.70:.4f}",
        f"below-cutoff reason got {below.reason!r}",
    )
    print("ok: cutoff is inclusive (>=); supported-but-low-score still escalates")


def test_refusal_and_malformed_responses_fail_closed() -> None:
    """Refusal, empty choices, and a missing parsed payload each fail closed —
    one call, faithful=False."""
    cases = {
        "judge_refusal": lambda: _completion(parsed=None, refusal="I can't help with that"),
        "judge_no_choices": lambda: _completion(choices=False),
        "judge_no_payload": lambda: _completion(parsed=None),
    }
    for tag, behavior in cases.items():
        d, fake = _run(behavior, "x")
        _check(d.faithful is False, f"{tag}: must fail closed, got {d!r}")
        _check(d.reason == f"unfaithful: {tag}", f"{tag}: reason got {d.reason!r}")
        _check(fake.calls == 1, f"{tag}: one call, got {fake.calls}")
    print("ok: refusal / empty-choices / missing-payload all fail closed in one call")


def test_score_clamped_to_unit_interval() -> None:
    """An out-of-range score from the model is clamped to [0,1] before the
    cutoff compare (no JSON-schema bound, matching DocumentMetadata)."""
    high, _ = _run(_judgment(True, 1.5), "x")
    _check(high.score == 1.0 and high.faithful is True, f"1.5 must clamp to 1.0, got {high!r}")
    low, _ = _run(_judgment(True, -0.3), "x")
    _check(low.score == 0.0 and low.faithful is False, f"-0.3 must clamp to 0.0, got {low!r}")
    print("ok: judge score clamped to [0,1] before the cutoff comparison")


def test_judge_model_selector() -> None:
    """`get_judge_model` reads JUDGE_MODEL, defaults to the cheap model, and does
    NOT chain through OPENAI_MODEL; the resolved model is what the call uses."""
    saved = {k: os.environ.get(k) for k in ("JUDGE_MODEL", "OPENAI_MODEL")}
    try:
        os.environ.pop("JUDGE_MODEL", None)
        os.environ["OPENAI_MODEL"] = "gpt-4o"  # must be IGNORED by the judge selector
        _check(
            get_judge_model() == DEFAULT_JUDGE_MODEL,
            f"unset JUDGE_MODEL must default to {DEFAULT_JUDGE_MODEL}, not OPENAI_MODEL",
        )
        _, fake = _run(_judgment(True, 0.9), "x")
        _check(
            cast(_FakeCompletions, fake.chat.completions).model_used == DEFAULT_JUDGE_MODEL,
            "the call must use the default judge model",
        )
        os.environ["JUDGE_MODEL"] = "claude-haiku-judge"
        _check(get_judge_model() == "claude-haiku-judge", "JUDGE_MODEL must win when set")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok: JUDGE_MODEL selector — cheap default, no OPENAI_MODEL chaining")


def test_judge_sampling_is_pinned_deterministic() -> None:
    """Issue #104: a fail-closed safety gate must not SAMPLE its verdict.

    The call pins `temperature=0` by default; the typed-out `JUDGE_TEMPERATURE=none`
    omits the parameter entirely (for a BYO judge model that rejects it, ADR-0006)
    rather than wedging that deployment at a 400 forever; a malformed, non-finite or
    out-of-range value falls back to the deterministic default instead of taking the
    gate offline.

    A BLANK value is pinned here explicitly, because it is the accidental state (a
    bare `-e JUDGE_TEMPERATURE`, an empty configMap value, a trailing
    `JUDGE_TEMPERATURE=` in a .env) and it must read as the default, never as the
    opt-out - an accident must not silently return a safety gate to sampling.
    """
    saved = os.environ.get("JUDGE_TEMPERATURE")
    try:
        os.environ.pop("JUDGE_TEMPERATURE", None)
        _check(
            get_judge_temperature() == 0.0,
            f"default judge temperature must be 0.0, got {get_judge_temperature()!r}",
        )
        _, fake = _run(_judgment(True, 0.9), "x")
        _check(
            cast(_FakeCompletions, fake.chat.completions).extra_kwargs
            == {"temperature": 0.0},
            "the faithfulness call must pin temperature=0 by default, got "
            f"{cast(_FakeCompletions, fake.chat.completions).extra_kwargs!r}",
        )

        for blank in ("", "   "):
            os.environ["JUDGE_TEMPERATURE"] = blank
            _check(
                get_judge_temperature() == 0.0,
                f"a blank JUDGE_TEMPERATURE ({blank!r}) must mean the pinned default, "
                f"not the un-pin escape hatch, got {get_judge_temperature()!r}",
            )
            _, fake = _run(_judgment(True, 0.9), "x")
            _check(
                cast(_FakeCompletions, fake.chat.completions).extra_kwargs
                == {"temperature": 0.0},
                f"a blank JUDGE_TEMPERATURE ({blank!r}) must still pin temperature=0, "
                f"got {cast(_FakeCompletions, fake.chat.completions).extra_kwargs!r}",
            )

        os.environ["JUDGE_TEMPERATURE"] = "none"
        _check(get_judge_temperature() is None, "'none' must mean: send no temperature")
        _, fake = _run(_judgment(True, 0.9), "x")
        _check(
            cast(_FakeCompletions, fake.chat.completions).extra_kwargs == {},
            "JUDGE_TEMPERATURE=none must omit the parameter entirely",
        )

        os.environ["JUDGE_TEMPERATURE"] = "not-a-number"
        _check(
            get_judge_temperature() == 0.0,
            "a malformed JUDGE_TEMPERATURE must fall back to the deterministic default",
        )

        # A value the judge API would 400 on fails both gates closed on every turn,
        # and the latch site cannot tell that from a deliberate escalate - so it
        # permanently silences the conversations it hits. It falls back rather than
        # shipping. (Unlike a model that rejects the PARAMETER, an out-of-range
        # value is the operator's own typo, so it is corrected here rather than
        # retried at call time.)
        for rejected in ("nan", "inf", "-inf", "50", "-0.5", "2.5"):
            os.environ["JUDGE_TEMPERATURE"] = rejected
            _check(
                get_judge_temperature() == 0.0,
                f"JUDGE_TEMPERATURE={rejected!r} is not a finite value the judge API "
                f"accepts and must fall back to the deterministic default, got "
                f"{get_judge_temperature()!r}",
            )

        # In-range numbers stay an explicit operator decision and are honoured.
        for accepted, want in (("0.7", 0.7), ("0", 0.0), ("2", 2.0)):
            os.environ["JUDGE_TEMPERATURE"] = accepted
            _check(
                get_judge_temperature() == want,
                f"JUDGE_TEMPERATURE={accepted!r} must be honoured as {want}, got "
                f"{get_judge_temperature()!r}",
            )
    finally:
        if saved is None:
            os.environ.pop("JUDGE_TEMPERATURE", None)
        else:
            os.environ["JUDGE_TEMPERATURE"] = saved
    print("ok: judge sampling pinned to temperature=0, with a documented escape hatch")


def test_context_and_draft_reach_the_judge() -> None:
    """The judge actually receives the draft and the chunk contents (so a 'zero'
    isn't a structurally-blind pass)."""
    _, fake = _run(_judgment(True, 0.9), "Returns within 30 days.")
    messages = cast(_FakeCompletions, fake.chat.completions).messages_used or []
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    _check("Returns within 30 days." in user, "draft must be in the judge prompt")
    _check("return window is 30 days" in user, "chunk content must be in the judge prompt")
    print("ok: draft + chunk content are passed to the judge")


def test_every_judge_failure_fails_closed_in_one_call() -> None:
    """No error shape is ever retried: every failure is one call, then escalate.

    `_judge_parse` makes exactly one call with no exception at all. A judge
    deployment that will not accept the pinned `temperature` - an OpenAI reasoning
    model refusing the parameter, or an endpoint refusing the value as out of its
    narrower range - is a CONFIGURATION fact an operator states once with
    `JUDGE_TEMPERATURE=none`, so its 400 is pinned here as just another fail-closed
    error rather than something inferred from the provider's wording at call time.
    """
    # Neutralize an ambient JUDGE_TEMPERATURE. `none` is the documented escape
    # hatch, so a developer shell may well export it, and this test's contract is
    # about the call count rather than the shell's state.
    saved_temp = os.environ.pop("JUDGE_TEMPERATURE", None)
    try:
        for label, exc in (
            ("other 400", _bad_request("Invalid schema for response_format")),
            ("rate limit", _api_error("Rate limit reached for requests", 429)),
            ("auth", _api_error("Incorrect API key provided", 401)),
            (
                "400 refusing the parameter",
                _bad_request(
                    "Unsupported parameter: 'temperature' is not supported with "
                    "this model."
                ),
            ),
            (
                "400 refusing the value",
                _bad_request("temperature: Input should be less than or equal to 1"),
            ),
        ):
            def boom(e: Exception = exc) -> Any:
                raise e

            d, fake = _run(boom, "Returns within 30 days.")
            _check(d.faithful is False, f"{label}: must fail CLOSED, got {d!r}")
            _check(
                d.reason == "unfaithful: judge_error",
                f"{label}: must stay a judge_error, got {d.reason!r}",
            )
            _check(fake.calls == 1, f"{label}: must not retry, got {fake.calls} calls")
    finally:
        if saved_temp is not None:
            os.environ["JUDGE_TEMPERATURE"] = saved_temp
    print("ok: every judge failure fails closed in exactly one call, never retried")


def test_decision_is_frozen() -> None:
    d = FaithfulnessDecision(faithful=True, supported=True, score=0.9, reason="faithful")
    try:
        d.faithful = False  # type: ignore[misc]
    except ValueError:
        print("ok: FaithfulnessDecision is frozen (immutable)")
        return
    raise AssertionError("FaithfulnessDecision must be frozen")


def main() -> int:
    tests = [
        test_grounded_draft_passes,
        test_ungrounded_draft_fails,
        test_judge_exception_fails_closed,
        test_cutoff_is_inclusive_and_enforced,
        test_refusal_and_malformed_responses_fail_closed,
        test_score_clamped_to_unit_interval,
        test_judge_model_selector,
        test_judge_sampling_is_pinned_deterministic,
        test_context_and_draft_reach_the_judge,
        test_every_judge_failure_fails_closed_in_one_call,
        test_decision_is_frozen,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} faithfulness-gate (US-048) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
