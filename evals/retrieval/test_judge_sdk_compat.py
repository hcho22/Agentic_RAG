"""Regression guard: the offline Claude judge's SDK must accept `temperature=0`.

Both weekly evals (RAGAS + escalation E7) score their drafts with the OFFLINE
cross-family Claude judge in `evals/retrieval/runner.py`
(`judge_answer` / `judge_answering`), which pins the verdict deterministic by
passing `temperature=0` to `anthropic ... messages.create(...)`.

The anthropic 1.0 major REMOVED the top-level `temperature` (and `top_p` /
`top_k`) kwarg from `messages.create()` - sampling controls are gone from the
1.x typed Messages surface entirely. With the historical loose `anthropic>=0.40.0`
in `evals/retrieval/requirements.txt`, CI resolved 1.2.0 (Python 3.11 clears its
`>=3.10` floor) and every judge call died with:

    TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature'

That crashed both weeklies at the judge call and produced no snapshot for weeks
(issues #124 / #125) - a pinned SAFETY / gate measurement silently going
unmeasured, exactly what CLAUDE.md invariant 12 forbids. The fix pins anthropic
to the last 0.x that expresses `temperature=0` natively; this guard fails if that
pin is ever loosened back to a range the temperature-less 1.x major can satisfy.

Two layers, mirroring the repo's "unit always runs / integration skips cleanly"
idiom:

  * ALWAYS (offline, no anthropic import - this is what runs in the slim
    `eval-harness-guards` job): parse the anthropic requirement in
    `evals/retrieval/requirements.txt` and assert its version specifier EXCLUDES
    the temperature-removing majors (1.0.0 and the observed 1.2.0) while still
    admitting the known-good pin (0.125.0). A loosened pin re-opens the crash, so
    the guard is red before it can reach a weekly run.

  * WHEN anthropic is importable (the weekly full env, or a local checkout that
    installed it): introspect the real `AsyncMessages.create` signature and
    assert every kwarg the two judge functions send - including `temperature` -
    is a parameter the installed SDK accepts. This ties the guard to the actual
    call shape, not just the declared pin. Skips cleanly when anthropic is absent.

Run:
    python -m evals.retrieval.test_judge_sdk_compat
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REQUIREMENTS = ROOT / "evals" / "retrieval" / "requirements.txt"

# The kwargs both offline judges pass to `messages.create` (runner.py
# judge_answer / judge_answering). `temperature` is the one the 1.x major
# dropped; the rest are here so this guard also catches a future SDK that
# renames or drops any other argument the judge relies on.
JUDGE_CREATE_KWARGS = frozenset(
    {"model", "max_tokens", "temperature", "tools", "tool_choice", "messages"}
)

# The known-good pin: the last 0.x that still expresses temperature=0 natively.
KNOWN_GOOD = "0.125.0"
# Versions that removed the temperature kwarg and MUST be excluded by the pin.
# 1.0.0 is the major boundary; 1.2.0 is the exact version CI resolved and crashed on.
TEMPERATURE_LESS = ("1.0.0", "1.2.0")


def _specifier_set(constraint: str):
    """Return a packaging SpecifierSet, from stdlib-adjacent sources only.

    The slim `eval-harness-guards` env does not install `anthropic`, and
    top-level `packaging` is not guaranteed there either - but `pip` always is,
    and it vendors packaging. Try the real package first, fall back to pip's.
    """
    try:
        from packaging.specifiers import SpecifierSet  # type: ignore
    except ImportError:  # pragma: no cover - depends on the ambient env
        from pip._vendor.packaging.specifiers import SpecifierSet  # type: ignore
    return SpecifierSet(constraint)


def _read_anthropic_constraint() -> str:
    """Extract the version specifier on the `anthropic` requirement line."""
    assert REQUIREMENTS.is_file(), f"requirements file not found: {REQUIREMENTS}"
    for raw in REQUIREMENTS.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"^anthropic\s*(?P<spec>[<>=!~].*)$", line, re.IGNORECASE)
        if m:
            return m.group("spec").replace(" ", "")
    raise AssertionError(
        "no `anthropic` requirement found in evals/retrieval/requirements.txt - "
        "the offline Claude judge depends on it; the pin must stay explicit"
    )


def _pin_excludes_temperatureless_sdk() -> None:
    constraint = _read_anthropic_constraint()
    spec = _specifier_set(constraint)

    assert KNOWN_GOOD in spec, (
        f"anthropic pin '{constraint}' must still admit the known-good "
        f"{KNOWN_GOOD} (the last 0.x that accepts temperature=0); it does not"
    )
    for bad in TEMPERATURE_LESS:
        assert bad not in spec, (
            f"anthropic pin '{constraint}' allows {bad}, which REMOVED the "
            f"`temperature` kwarg from messages.create(). That is the exact drift "
            f"that crashed both weekly evals at the deterministic judge call "
            f"(issues #124/#125). Keep the pin below the 1.0 major."
        )
    print(
        f"  requirements pin '{constraint}' admits {KNOWN_GOOD}, "
        f"excludes {', '.join(TEMPERATURE_LESS)}"
    )


def _installed_sdk_accepts_judge_kwargs() -> None:
    try:
        import anthropic  # noqa: F401
        from anthropic.resources.messages import AsyncMessages
    except ImportError:
        print("  (anthropic not installed - signature check skipped, pin check stands)")
        return

    import inspect

    params = set(inspect.signature(AsyncMessages.create).parameters)
    missing = sorted(JUDGE_CREATE_KWARGS - params)
    assert not missing, (
        f"installed anthropic {getattr(anthropic, '__version__', '?')} "
        f"AsyncMessages.create() does not accept {missing} - the offline judge "
        f"passes these and would crash with 'unexpected keyword argument'. This is "
        f"the anthropic-1.x temperature-removal regression (issues #124/#125)."
    )
    print(
        f"  installed anthropic {anthropic.__version__} accepts every judge kwarg "
        f"({', '.join(sorted(JUDGE_CREATE_KWARGS))})"
    )


def main() -> int:
    print("Judge SDK compatibility guard (temperature=0 deterministic judging):")
    _pin_excludes_temperatureless_sdk()
    _installed_sdk_accepts_judge_kwargs()
    print("\nPASS: judge SDK compatibility guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
