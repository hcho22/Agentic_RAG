"""Regression test (US-120): the generator must refuse a truncated/empty answer.

gpt-5.6-luna is a reasoning model that bills reasoning tokens against
`max_completion_tokens` (GENERATION_MAX_TOKENS=400), so a turn whose reasoning
exhausts the budget returns `finish_reason='length'` with truncated or blank
`message.content`. The historical `return content or ""` then handed an empty
answer to the judge, which scored it as a real low faithfulness/helpfulness
result - a budget cutoff wearing the exact shape of a real generation regression.
That is what CLAUDE.md invariant 12 forbids: "measured nothing" must never be
reportable as a measurement. `generate_answer` now raises
`TruncatedGenerationError` instead, and this test pins each branch:

  * finish_reason == 'length' (with or without partial content) -> raises;
  * blank/whitespace/None content at any finish_reason -> raises;
  * a genuine, non-truncated answer -> returned verbatim (the guard does not
    swallow real output).

Everything here is offline and always runs: no DB, no secrets, no network. The
OpenAI client is a hand-built fake returning a canned chat completion, so the
guard is exercised without a live Luna call.

Run:
    python -m evals.retrieval.test_us120_generation_guard
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.retrieval.runner import (  # noqa: E402
    TruncatedGenerationError,
    generate_answer,
)


class _FakeCompletions:
    """Stands in for `client.chat.completions`, returning one canned response."""

    def __init__(self, content: str | None, finish_reason: str) -> None:
        self._content = content
        self._finish_reason = finish_reason

    async def create(self, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self._content),
                    finish_reason=self._finish_reason,
                )
            ]
        )


def _fake_client(content: str | None, finish_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(content, finish_reason))
    )


def _expect_raises(content: str | None, finish_reason: str, *, what: str) -> None:
    client = _fake_client(content, finish_reason)
    try:
        asyncio.run(generate_answer(client, "How much is a widget?", "ctx"))
    except TruncatedGenerationError as e:
        message = str(e)
        assert "How much is a widget?" in message, (
            f"{what}: message must name the offending question so the CI log is "
            f"actionable on its own; got: {message}"
        )
        return
    raise AssertionError(f"{what}: expected TruncatedGenerationError, none raised")


def _truncation_checks() -> None:
    # A reasoning burn that leaves nothing usable: length-stop, no content.
    _expect_raises(None, "length", what="finish_reason=length, content=None")
    # Length-stop that emitted a partial fragment is still a truncated turn -
    # the answer is incomplete, so it must not be scored as a real result.
    _expect_raises("The price is", "length", what="finish_reason=length, partial")
    # A clean stop that still handed back nothing usable.
    _expect_raises(None, "stop", what="finish_reason=stop, content=None")
    _expect_raises("", "stop", what="finish_reason=stop, content=empty")
    _expect_raises("   \n\t ", "stop", what="finish_reason=stop, content=whitespace")
    print("  truncated/empty generation raises TruncatedGenerationError")


def _happy_path_check() -> None:
    # The load-bearing negative: a genuine, complete answer must pass through
    # verbatim - the guard must not swallow real output.
    client = _fake_client("A widget is quoted per-case by customer service.", "stop")
    answer = asyncio.run(generate_answer(client, "How much is a widget?", "ctx"))
    assert answer == "A widget is quoted per-case by customer service.", answer
    print("  a genuine answer passes through untouched")


def main() -> int:
    print("US-120 generation guard (truncated/empty answer):")
    _truncation_checks()
    _happy_path_check()
    print("\nPASS: US-120 generation guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
