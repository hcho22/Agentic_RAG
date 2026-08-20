"""US-121 validation test: boot warning when a temperature-refusing answerer
would be inherited by an unpinned text-generation helper.

The answerer (`OPENAI_MODEL`) is migrating to a reasoning model that forces
`temperature=1` and refuses an explicit `temperature`. Four helpers fall through
their own selector env var to `OPENAI_MODEL`, and three send a hardcoded
`temperature=0.0`; if any selector is unset the helper inherits the answerer and
400s on its first real request. `warn_if_answerer_rejects_temperature` surfaces
that at boot.

Unlike the judge warning this is NOT widget-scoped - the helpers run on the core
knowledge-assistant path every deploy runs (AGENTS.md invariant 10 does not
apply). This test reads only the process environment and captures the escalation
logger; no DB, no network, no secrets, so it runs anywhere.

Run:
    python -m backend.test_us121_answerer_temp_warn
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from escalation import (  # noqa: E402
    _ANSWERER_INHERITING_HELPERS,
    warn_if_answerer_rejects_temperature,
)

_HELPER_ENVS = [env for env, _name in _ANSWERER_INHERITING_HELPERS]


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _run_with_env(answerer: object, helper_values: dict[str, object]) -> list[str]:
    """Set OPENAI_MODEL + the four helper selectors, invoke the warning, and
    return the captured messages. `None` values mean "unset". Restores env."""
    keys = ("OPENAI_MODEL", *_HELPER_ENVS)
    saved = {k: os.environ.get(k) for k in keys}
    handler = _Capture()
    log = logging.getLogger("agentic_rag.escalation")
    log.addHandler(handler)
    try:
        for k in keys:
            os.environ.pop(k, None)
        if answerer is not None:
            os.environ["OPENAI_MODEL"] = str(answerer)
        for env, val in helper_values.items():
            if val is not None:
                os.environ[env] = str(val)
        warn_if_answerer_rejects_temperature()
        return [r.getMessage() for r in handler.records]
    finally:
        log.removeHandler(handler)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_unpinned_helpers_under_refusing_answerer_warn_once() -> None:
    """Validation step 1: a Luna-class answerer with all four selectors unset
    logs ONE warning naming all four helpers and their env vars."""
    for answerer in ("gpt-5.6-luna", "gpt-5-mini", "o3", "O4-MINI"):
        msgs = _run_with_env(answerer, {env: None for env in _HELPER_ENVS})
        _check(
            len(msgs) == 1,
            f"OPENAI_MODEL={answerer!r} with all helpers unset must warn exactly "
            f"once, got {len(msgs)}: {msgs!r}",
        )
        message = msgs[0]
        for env in _HELPER_ENVS:
            _check(
                env in message,
                f"the warning must name env var {env!r}, got {message!r}",
            )
        _check(
            "NOT an observed refusal" in message and "VERIFY" in message,
            "the warning must be conditional and tell the operator to verify, got "
            f"{message!r}",
        )


def test_all_pinned_helpers_are_silent() -> None:
    """Validation step 2: all four selectors set to an accepting model -> silent,
    even though the answerer itself refuses. An explicit value is the operator's
    choice and is skipped."""
    msgs = _run_with_env(
        "gpt-5.6-luna", {env: "gpt-4o-mini" for env in _HELPER_ENVS}
    )
    _check(not msgs, f"all helpers pinned must be silent, got {msgs!r}")


def test_partial_pin_names_only_the_unpinned() -> None:
    """A helper with an explicit selector is skipped; an unset one is named. The
    single warning lists exactly the unpinned helpers."""
    pinned = _HELPER_ENVS[0]
    values = {env: ("gpt-4o-mini" if env == pinned else None) for env in _HELPER_ENVS}
    msgs = _run_with_env("gpt-5.6-luna", values)
    _check(len(msgs) == 1, f"one warning expected, got {msgs!r}")
    message = msgs[0]
    _check(
        pinned not in message,
        f"the pinned helper {pinned!r} must not be named, got {message!r}",
    )
    for env in _HELPER_ENVS[1:]:
        _check(env in message, f"unpinned {env!r} must be named, got {message!r}")


def test_non_refusing_answerer_is_silent() -> None:
    """Validation step 3: a non-refusing answerer with helpers unset -> silent.
    Includes the `-chat` non-reasoning exclusion and an unrecognised name (best-
    effort: an unknown refuser gets no warning)."""
    for answerer in (
        None,  # OPENAI_MODEL unset -> default gpt-4o-mini
        "gpt-4o-mini",
        "gpt-5-chat-latest",
        "claude-sonnet-5",
        "my-o3-lookalike",
    ):
        msgs = _run_with_env(answerer, {env: None for env in _HELPER_ENVS})
        _check(
            not msgs,
            f"OPENAI_MODEL={answerer!r} must NOT warn (non-refusing / unknown / "
            f"-chat excluded), got {msgs!r}",
        )


def main() -> int:
    tests = [
        test_unpinned_helpers_under_refusing_answerer_warn_once,
        test_all_pinned_helpers_are_silent,
        test_partial_pin_names_only_the_unpinned,
        test_non_refusing_answerer_is_silent,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} US-121 answerer-temperature-warning test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
