"""Issue #104: the answer-completeness gate's RUBRIC, as opposed to its plumbing.

`test_answer_gate.py` drives `answer_gate` through a fake judge that returns a
canned verdict. That pins the plumbing — exactly-one-call, fail-closed, clamping,
cutoff — and it is deliberately blind to what the rubric actually DISCRIMINATES,
because the fake never reads the prompt. Until this file there was no layer below
the weekly E7 sweep where a rubric change could be pinned at all, which is why the
issue-#104 gap survived from the gate's authoring (PR #99) to the 2026-08-03 sweep.

THE GAP THIS FILE PINS
----------------------
The original rubric caught the shape that ANNOUNCES itself — a draft saying it
lacks the information. It missed the shape where the CORPUS's own answer is a
deferral. `db_seed/corpus/returns-process.md:33` says return shipping over 20 lbs
is "quoted per-case"; `db_seed/corpus/warranty-terms.md:29` says a book warranty
claim past 30 days is "at the discretion of customer service". A draft restating
either is faithful, fluent, and lands exactly on the slot the question asked
about, so the gate read the slot as filled and auto-sent. The customer still has
no fee and no warranty period. `e7-p3-05` and `e7-p3-11` false-resolved that way.

Those two shapes are ORTHOGONAL, and both must be pinned:
  * whether the draft ADMITS ignorance is a fact about the drafter;
  * whether the customer ends up HOLDING the requested value is a fact about the
    answer.
Keying on the first alone made the send/escalate verdict ride on whether
`gpt-4o-mini` happened to append "Therefore, I don't have that information" — an
observed coin flip across runs of the same row.

LIVE CALLS vs RECORDED FIXTURES — the deliberate choice
-------------------------------------------------------
This file makes LIVE model calls in its integration layer, and records nothing.

Recorded fixtures were considered and rejected for the discrimination cases. A
recorded judge response is a canned verdict replayed from disk, which is exactly
what the `_FakeJudge` already does: it would re-pin the plumbing under a new name
while still never exercising the rubric. A fixture can only tell you that the
model said X on the day you recorded it, so the one failure mode this layer exists
to catch — a rubric edit that changes what the model concludes — is precisely the
one it would be blind to.

What that costs, stated plainly:
  * money and latency (~48 calls per full run, on the two cheap judge models);
  * a dependency on network + API keys, so it CANNOT be part of the always-run
    unit layer;
  * mild non-determinism. Mitigated, not hand-waved: both judges are now pinned
    to temperature 0 (issue #104's other half), each case is asserted UNANIMOUS
    over `_REPS` calls, and every case in the table was measured stable at 5/5
    during the investigation. A single flip is therefore a real signal and fails
    the test rather than being retried away.

The offline half stays fixture-free in a different sense: it reads the two rubric
strings out of the source and asserts they carry the same rules. That needs no
model at all, so it runs in the always-on layer and is what actually catches the
two implementations drifting apart.

Layers (the project convention — see CLAUDE.md "How to test"):
  * unit layer, ALWAYS runs, no network/keys/DB: rubric lockstep + rule presence.
  * integration layer, SKIPS CLEANLY without keys: live discrimination on both
    implementations.

Run:
    python -m backend.test_answer_gate_rubric
"""

from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from escalation import (  # noqa: E402
    _ANSWER_JUDGE_SYSTEM_PROMPT,
    AnswerJudgment,
    DEFAULT_ANSWER_CUTOFF,
    answer_gate,
    judge_failure_tag,
)

# How many times each live case is judged. Every case must come back UNANIMOUS.
_REPS = 3

# The offline mirror lives in the evals package, which pulls in asyncpg / yaml /
# jwt at import time — dependencies the backend unit layer must not require. Read
# its rubric out of the source with `ast` instead: these are plain literals, so
# this needs nothing but the stdlib and still reads the REAL shipped text.
_RUNNER_SRC = ROOT / "evals" / "retrieval" / "runner.py"


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _literal_from_source(path: Path, name: str) -> Any:
    """Evaluate a module-level literal assignment without importing the module."""
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found as a literal assignment in {path}")


def _offline_rubric() -> str:
    return _literal_from_source(_RUNNER_SRC, "ANSWER_JUDGE_PROMPT_TEMPLATE")


def _offline_tool_description() -> str:
    tool = _literal_from_source(_RUNNER_SRC, "ANSWER_JUDGE_TOOL")
    return tool["input_schema"]["properties"]["answers"]["description"]


def _runtime_tool_description() -> str:
    return AnswerJudgment.model_fields["answers"].description or ""


# The rules BOTH implementations must state. Phrased as the substrings they share,
# so the check survives the small wording differences between the two prompts
# while still failing loudly if one of them loses a rule the other keeps.
_SHARED_RULES = {
    "non-answer verdict": "does NOT answer the question",
    "admits it lacks the information": "have the information",
    "cannot help": "cannot help",
    "defers to a human": "the customer to a human",
    "answers a different question": "DIFFERENT question",
    "judges answering only, not grounding": "not grounding, tone, or politeness",
    # --- the issue-#104 addition ---
    "corpus's answer is 'quoted case-by-case'": "quoted case-by-case",
    "corpus's answer is 'someone's discretion'": "someone's discretion",
    "corpus's answer is unpublished": "otherwise not published",
    "accuracy of the deferral is irrelevant": (
        "however accurately or confidently the reply states that policy"
    ),
}


# --- unit layer (always runs) ---------------------------------------------


def test_both_rubrics_state_every_shared_rule() -> None:
    """The runtime gate and its offline E7 mirror must state the SAME rules.

    They are two separate strings in two packages, kept in step by hand (the
    cross-family independence is deliberate; see the block comment above
    `ANSWER_JUDGE_PROMPT_TEMPLATE`). Drift between them silently turns the weekly
    E7 false-resolve number into a measurement of something the buyer does not
    ship, which is how the two were found disagreeing on 5 of 17 probes.
    """
    runtime = _ANSWER_JUDGE_SYSTEM_PROMPT
    offline = _offline_rubric()
    for label, fragment in _SHARED_RULES.items():
        _check(
            fragment in runtime,
            f"the RUNTIME rubric (escalation._ANSWER_JUDGE_SYSTEM_PROMPT) no longer "
            f"states the rule {label!r} (missing {fragment!r})",
        )
        _check(
            fragment in offline,
            f"the OFFLINE mirror (runner.ANSWER_JUDGE_PROMPT_TEMPLATE) no longer "
            f"states the rule {label!r} (missing {fragment!r}) — the two rubrics "
            "have drifted apart",
        )
    print(f"ok: both rubrics state all {len(_SHARED_RULES)} shared rules")


def test_both_tool_schemas_describe_the_disposition_case() -> None:
    """The structured-output description the model reads must agree with the
    rubric, in both implementations — a schema that still says a deferral counts
    as an answer would pull against the prompt."""
    for what, description in (
        ("runtime AnswerJudgment.answers", _runtime_tool_description()),
        ("offline submit_answering.answers", _offline_tool_description()),
    ):
        for fragment in ("case-by-case", "discretionary", "unpublished"):
            _check(
                fragment in description,
                f"{what} must describe the issue-#104 disposition case "
                f"(missing {fragment!r}): {description!r}",
            )
    print("ok: both tool schemas describe the disposition case")


# --- integration layer (skips cleanly without keys) ------------------------

# question, draft, must_answer, why. The drafts are the ones actually observed in
# the 2026-08-03 sweep and its local re-runs, not invented paraphrases.
_CASES: list[tuple[str, str, str, bool, str]] = [
    (
        "p3-05 quoted-per-case",
        "What is the return shipping fee for a return over 20 lbs?",
        "The return shipping fee for returns over 20 lbs is quoted on a per-case basis.",
        False,
        "the corpus's own answer is a deferral; the customer still has no fee",
    ),
    (
        "p3-05 quoted-per-case, verbose",
        "What is the return shipping fee for a return over 20 lbs?",
        "The return shipping fee for returns over 20 lbs is quoted per-case, meaning "
        "the specific amount is not fixed and will vary based on the situation.",
        False,
        "same shape, restated at length; length must not buy a pass",
    ),
    (
        "p3-11 at-someone's-discretion",
        "What is the warranty period for a book with a printing defect after 30 days?",
        "Books carry a 30-day warranty against printing defects (missing pages, "
        "blurred ink, binding failure). Beyond 30 days, book warranty claims are at "
        "the discretion of customer service. Therefore, the warranty period for a "
        "book with a printing defect after 30 days is not specified and is subject "
        "to customer service judgment.",
        False,
        "the second flavour: the document names a human decision-maker",
    ),
    (
        "p3-11 at-someone's-discretion, NO self-disclaimer",
        "What is the warranty period for a book with a printing defect after 30 days?",
        "Books carry a 30-day warranty against printing defects. Beyond 30 days, "
        "book warranty claims are at the discretion of customer service.",
        False,
        "THE REGRESSION GUARD: identical substance, no volunteered disclaimer. If "
        "this one passes, the gate is back to keying on the drafter's phrasing",
    ),
    (
        "announced ignorance (the original issue-#97 shape)",
        "What is the warranty period for jewelry?",
        "I don't have that information.",
        False,
        "the shape the gate was built for; must stay caught",
    ),
    (
        "answerable: a published fee",
        "What is the return shipping fee for an item between 5 and 20 pounds that I "
        "changed my mind about?",
        "The return shipping fee for an item between 5 and 20 pounds that you "
        "changed your mind about is $14.95.",
        True,
        "OPPOSITE DIRECTION: a real published figure must still auto-resolve, so "
        "an over-tightened rubric fails here instead of quietly killing deflection",
    ),
    (
        "answerable: a published warranty period",
        "How long is the electronics warranty?",
        "The electronics warranty is 12 months against manufacturing defects, "
        "starting on the order's `shipped_at` date.",
        True,
        "opposite direction, second instance",
    ),
    (
        "answerable: a published window",
        "Within how many days of the shipped_at date can I request a refund?",
        "You may request a refund within 30 days of the order's `shipped_at` date.",
        True,
        "opposite direction, third instance",
    ),
]


def _skip(reason: str) -> None:
    print(f"SKIP (live rubric layer): {reason}")


def test_live_rubric_discrimination() -> None:
    """Exercise the REAL rubric, on both implementations, against labelled cases.

    Skips cleanly when a key or package is absent so the unit layer above still
    runs anywhere. Requires unanimity over `_REPS` calls per case: both judges are
    pinned to temperature 0, so a split verdict is a finding, not a flake.

    A judge that was CALLED AND FAILED is a third outcome, distinct from both a
    clean skip and a verdict: it is reported as UNMEASURED and fails the test. The
    runtime gate fails closed, so a dead judge answers False on every case and
    would otherwise print `ok` for every `must_answer=False` row while exercising
    no rubric at all - invariant 12's "measured nothing must never be reportable as
    a measurement", in the one layer whose entire job is to measure the rubric.
    """
    openai_key = os.environ.get("OPENAI_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not openai_key and not anthropic_key:
        _skip("neither OPENAI_API_KEY nor ANTHROPIC_API_KEY is set")
        return

    failures: list[str] = []

    # A judge verdict, plus the `JUDGE_FAILURE_TAGS` entry naming a judge that was
    # CALLED AND FAILED (`None` when the judge actually reached a verdict). This
    # alias is EVALUATED, not deferred like an annotation, so it spells the union
    # the `typing` way rather than with `|`.
    Judge = Callable[[str, str], Awaitable[Tuple[bool, Optional[str]]]]

    def _build_impls() -> list[tuple[str, Judge]]:
        impls: list[tuple[str, Judge]] = []
        if openai_key:
            from openai import AsyncOpenAI

            oc = AsyncOpenAI(api_key=openai_key)

            async def runtime(question: str, draft: str) -> tuple[bool, str | None]:
                # `answer_gate` fails CLOSED, so an expired key, a rate limit, a
                # timeout or a network blip all return answers=False - the same
                # value a correct non-answer verdict returns. Reading only
                # `.answers` would print `ok` for every must_answer=False case
                # while zero rubric evaluation happened, which is exactly the
                # invariant-12 shape the project forbids. The `reason` is what
                # separates the two, so it is carried out of here.
                decision = await answer_gate(
                    oc, question, draft, DEFAULT_ANSWER_CUTOFF
                )
                return decision.answers, judge_failure_tag(decision.reason)

            impls.append(("runtime", runtime))
        if anthropic_key:
            try:
                import anthropic
            except ImportError:
                print(
                    "note: ANTHROPIC_API_KEY is set but the `anthropic` package is "
                    "not installed; skipping the OFFLINE mirror half"
                )
            else:
                # The offline mirror is only imported HERE, inside the live layer,
                # so the always-run unit layer above never needs the eval deps.
                sys.path.insert(0, str(ROOT))
                from evals.retrieval.runner import judge_answering

                ac = anthropic.AsyncAnthropic(api_key=anthropic_key)

                # The offline mirror RAISES on a failed/unparseable judge call
                # rather than failing closed, so it cannot silently report a dead
                # judge as a verdict and never needs a failure tag.
                async def offline(question: str, draft: str) -> tuple[bool, str | None]:
                    return await judge_answering(ac, question, draft), None

                impls.append(("offline", offline))
        return impls

    async def run() -> None:
        impls = _build_impls()
        if not impls:
            _skip("no usable judge implementation")
            return

        for label, question, draft, must_answer, why in _CASES:
            for impl_name, judge in impls:
                results = await asyncio.gather(
                    *[judge(question, draft) for _ in range(_REPS)]
                )

                # A judge that was called and failed measured NOTHING, so this case
                # is UNMEASURED - never a legitimate non-answer verdict, and never
                # an `ok`.
                judge_failures = sorted({tag for _, tag in results if tag})
                if judge_failures:
                    print(
                        f"  UNMEASURED [{impl_name:>7}] {label}: the judge was "
                        f"called and failed ({', '.join(judge_failures)})"
                    )
                    failures.append(
                        f"[{impl_name}] {label}: HARNESS FAILURE - the judge was "
                        f"called and failed ({', '.join(judge_failures)}), so this "
                        "case exercised no rubric at all. The gate fails closed, so "
                        "a dead judge reports answers=False and would otherwise read "
                        "as the rubric correctly rejecting a non-answer. Fix the "
                        "judge credentials/connectivity and re-run."
                    )
                    continue

                verdicts = [answers for answers, _ in results]
                unanimous = len(set(verdicts)) == 1
                got = verdicts[0] if unanimous else verdicts
                ok = unanimous and verdicts[0] is must_answer
                print(
                    f"  {'ok ' if ok else 'FAIL'} [{impl_name:>7}] {label}: "
                    f"answers={got} (want {must_answer})"
                )
                if not unanimous:
                    failures.append(
                        f"[{impl_name}] {label}: SPLIT verdict {verdicts} over "
                        f"{_REPS} calls at temperature 0 — the gate is sampling"
                    )
                elif verdicts[0] is not must_answer:
                    failures.append(
                        f"[{impl_name}] {label}: answers={verdicts[0]}, want "
                        f"{must_answer} — {why}"
                    )

    asyncio.run(run())
    _check(not failures, "live rubric discrimination failed:\n  - " + "\n  - ".join(failures))
    print("ok: the live rubric separates deferral-shaped non-answers from real answers")


def main() -> int:
    tests = [
        test_both_rubrics_state_every_shared_rule,
        test_both_tool_schemas_describe_the_disposition_case,
        test_live_rubric_discrimination,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} answer-gate RUBRIC (issue #104) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
