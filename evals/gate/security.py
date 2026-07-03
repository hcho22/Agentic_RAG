"""US-102 (Epic F, ADR-0005): security invariants as binary asserts.

Security-class outputs are pinned ``fail`` (US-102) and evaluated as **binary
``assert``s**, never a threshold comparison a buyer can loosen. This module holds
the E4 zero-leak binary assert over the runner's ``security_no_access`` table:

    for every ``no_access`` cell, ``security_no_access[filter][mode] == 1.0``

i.e. *every* ``no_access`` run must have retrieved **zero** gold chunks. Any cell
below ``1.0`` means at least one run under a no-access viewer returned gold — a
leak — and is a violation that fails the run non-zero, **independent of any buyer
verdict config** (there is no knob; see :mod:`evals.gate.declaration`).

This closes a real gap: ``runner.py`` computed and *rendered* ``security_no_access``
in the summary but never hard-asserted it to be ``1.0``. Wiring this evaluator into
the runner's exit path makes an E4 leak fail the build the same way an E6
cross-workspace leak already does.

The sibling security invariants are asserted at their own call sites and are NOT
re-implemented here (this module owns only the E4 table):

* **E6** — the cross-workspace leak assert lives in ``runner.py::amain`` (the
  ``e6_result.leak_detected`` block) over ``evals/retrieval/e6.py``.
* **AU4** — deterministic ``assert == 0`` cases in
  ``backend/test_au4_auth_attacks.py``.
* **E7-P1b** — customer-facing P1b ≡ P1a, asserted on the E7 escalation runner.

All four are enumerated by :func:`evals.gate.classes.security_outputs`; this
module cross-links E4 back to that registry so the two cannot silently drift.

Run the tests: ``python -m evals.gate.test_pinned_security``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from evals.gate.classes import SecurityGateError, gate_class

# The registered gate-class name for the E4 zero-leak invariant (US-101). The E4
# security invariant IS the `security_no_access` table reading all-1.0.
E4_ZERO_LEAK = "E4_zero_leak"

# The invariant value every `no_access` cell must hold: a fraction of 1.0 means
# 100% of no-access runs returned zero gold. This is a binary assert, not a
# tunable floor — a buyer cannot loosen it (US-102 AC2).
_ZERO_LEAK_TARGET = 1.0

# Defensive link to the registry: E4 must be a security-class invariant. If a
# future edit reclassifies it, fail loudly at import rather than silently
# evaluate a "security" assert against a now-tunable output.
if not gate_class(E4_ZERO_LEAK).is_security:  # pragma: no cover - import guard
    raise SecurityGateError(
        f"{E4_ZERO_LEAK!r} must be a security-class invariant for the E4 "
        "zero-leak binary assert to be a pinned gate (US-101/US-102)."
    )


@dataclass(frozen=True)
class SecurityViolation:
    """One breached security invariant cell.

    ``output``   — the registered security output that was breached (``E4_zero_leak``).
    ``filter``   — the retrieval filter strategy (``pre_filter`` / ``post_filter``).
    ``mode``     — the retrieval mode (``vector`` / ``keyword`` / ``hybrid`` / …).
    ``observed`` — the fraction actually seen (< 1.0 ⇒ some no-access run leaked gold).
    ``detail``   — a human-readable one-liner for the log / non-zero exit message.
    """

    output: str
    filter: str
    mode: str
    observed: float
    detail: str


def no_access_exercised(security_no_access: "Mapping[str, Mapping[str, float]]") -> bool:
    """True when the E4 ``no_access`` invariant was actually exercised.

    ``runner.py`` writes a cell only for viewers/modes that ran, so an empty (or
    all-empty) table means the ``no_access`` viewer was not part of this
    invocation (e.g. ``--viewers full``). The invariant only asserts where it
    runs (US-102 AC3), so a caller uses this to distinguish "clean" from "not
    exercised".
    """
    return any(bool(by_mode) for by_mode in security_no_access.values())


def e4_structurally_blind(
    viewers: "Iterable[str]",
    security_no_access: "Mapping[str, Mapping[str, float]]",
) -> bool:
    """True when the E4 zero-leak invariant would pass **vacuously**.

    The ``no_access`` viewer was requested for this run, yet its sweep produced
    no cells, so :func:`check_no_access_zero_leak` has nothing to assert and the
    pinned gate would exit ``0`` without ever exercising the invariant — the
    exact "gate silently off" failure US-102 forbids (a dropped ``no_access``
    block, a misconfigured sweep). The runner treats this as a hard fail,
    mirroring E6's structural-blindness guard.

    When ``no_access`` was NOT requested (e.g. ``--viewers full``) the invariant
    legitimately does not apply, so this returns ``False`` — a skip, not a fail
    (US-102 AC3: the invariant only asserts where it runs).
    """
    return "no_access" in viewers and not no_access_exercised(security_no_access)


def check_no_access_zero_leak(
    security_no_access: "Mapping[str, Mapping[str, float]]",
) -> "list[SecurityViolation]":
    """Binary-assert the E4 zero-leak invariant; return every breached cell.

    For every ``security_no_access[filter][mode]`` cell, the fraction of
    no-access runs that returned zero gold MUST be ``1.0``. Any cell below ``1.0``
    is a leak and yields a :class:`SecurityViolation`. Returns an empty list when
    the invariant holds (or was not exercised) — the caller decides how to act
    (the runner logs each violation and exits non-zero).

    This is deliberately a ``== 1.0`` binary check, not a ``>= floor`` comparison:
    there is no tolerance to tune (US-102 AC2). The table's fractions are already
    rounded to 4 dp by the runner, so a clean cell is exactly ``1.0``.
    """
    violations: list[SecurityViolation] = []
    for filt, by_mode in security_no_access.items():
        for mode, observed in by_mode.items():
            value = float(observed)
            if value < _ZERO_LEAK_TARGET:
                violations.append(
                    SecurityViolation(
                        output=E4_ZERO_LEAK,
                        filter=str(filt),
                        mode=str(mode),
                        observed=value,
                        detail=(
                            f"E4 zero-leak breached: no_access/{mode}/{filt} "
                            f"returned gold on {(1.0 - value) * 100:.1f}% of runs "
                            f"(security_no_access={value:.4f}, must be 1.0)"
                        ),
                    )
                )
    return violations


def assert_no_access_zero_leak(
    security_no_access: "Mapping[str, Mapping[str, float]]",
) -> None:
    """Raise :class:`SecurityGateError` if the E4 zero-leak invariant is breached.

    The raising counterpart to :func:`check_no_access_zero_leak`, for callers that
    prefer exception control flow (tests, a future per-PR gate step, US-105). A
    security breach is never downgradable to a warning — it raises.
    """
    violations = check_no_access_zero_leak(security_no_access)
    if violations:
        joined = "; ".join(v.detail for v in violations)
        raise SecurityGateError(
            f"E4 zero-leak security invariant breached ({len(violations)} cell(s)): "
            f"{joined}. This is a pinned `fail` and cannot be downgraded (US-102)."
        )
