"""US-101 (Epic F, ADR-0005): classify every eval output into a gate class.

This module is the single authoritative registry that tags each eval output as
either a **`security` invariant** or a **`quality` metric**, plus an orthogonal
**determinism** flag (US-104/US-105 use the determinism axis to decide what may
block a merge). It exists so the downstream gate can apply two very different
postures without a buyer ever being able to blur them:

- `security` outputs (E4 zero-leak, E6 workspace boundary, AU4 API-layer
  auth-attacks, E7 P1b non-disclosure) are **pinned `fail`**. They carry NO
  loudness knob at all — the only way to stop one is to *delete* its eval, a
  loud tracked diff (US-102). Querying a security output's loudness knob is a
  programming error, so it RAISES rather than returning a silent default.
- `quality` outputs (`recall_at_k` / `mrr` / `ndcg_at_5`, the four RAGAS scores,
  escalation `deflection_rate` / `false_escalate_rate`) are **tunable** — a
  per-suite `off|comment|fail` loudness knob (US-104) rides over their existing
  `red`/`yellow` severity.
- `false_resolve` **straddles**: it is a `quality` metric (the buyer picks the
  *value* of its ceiling, US-050) but its ceiling breach is a **pinned
  invariant** (`straddle="ceiling_is_invariant"`) — US-104's verdict layer
  (`evals/gate/verdict.py`) treats a breach as a hard fail regardless of the
  escalation suite's loudness knob. So the buyer sets the tolerance; they cannot
  configure the gate to *ignore* a breach of their own tolerance.

Design note — this registry is deliberately **import-safe and dependency-light**.
It is a pinned-invariant module that other stories build on, so it must always
import cleanly. It pulls the four RAGAS metric names from the stdlib-only
``evals.retrieval.ragas`` (avoiding drift on that list) but it does NOT import
the heavy runner / e7_runner modules (asyncpg / openai / httpx) — the recall
``k`` values mirror ``runner.py::RECALL_KS`` as a pinned local tuple, and the
escalation-rate names mirror ``e7_runner.py`` by name. ``test_classes.py`` carries
a best-effort drift guard that cross-checks those against the real constants when
the heavy modules happen to be importable.

The AC's word "class" is spelled ``category`` here — ``class`` is a reserved
Python keyword, so ``gate_class("E4_zero_leak").category == "security"`` is the
executable form of the spec's ``.class``.

Run the tests: ``python -m evals.gate.test_classes``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Stdlib-only import (ragas.py pulls in nothing heavy) — the four canonical RAGAS
# metric names, sourced from their definition so this registry can never drift
# from the metrics the RAGAS suite actually emits.
from evals.retrieval.ragas import RAGAS_METRICS

# ---------------------------------------------------------------------------
# Axis vocabularies. Kept as plain string constants (not enums) so a gate
# declaration (US-103) can round-trip them through YAML without a codec.
# ---------------------------------------------------------------------------

# The gate CLASS (the AC's `class`, renamed off the reserved keyword).
SECURITY = "security"
QUALITY = "quality"

# The orthogonal DETERMINISM axis (US-104/US-105): deterministic gates are pure
# arithmetic / binary asserts and may block a merge per-PR; non_deterministic
# gates are LLM-judged and live only on scheduled runs.
DETERMINISTIC = "deterministic"
NON_DETERMINISTIC = "non_deterministic"

# The straddle marker: `false_resolve` is quality, but its ceiling is pinned.
CEILING_IS_INVARIANT = "ceiling_is_invariant"

# The tunable loudness verdicts (US-104). Security outputs carry NONE of these —
# they are structurally pinned `fail`. The default for a quality output is
# `comment` (matches the repo's PR-comment-only posture, US-104).
DEFAULT_LOUDNESS = "comment"
_LOUDNESS_VALUES = frozenset({"off", "comment", "fail"})

# The recall `k` values, mirroring runner.py::RECALL_KS. Pinned locally so this
# registry stays import-light (runner.py pulls in asyncpg/openai/httpx); the drift
# guard lives in test_classes.py.
_RECALL_KS: "tuple[int, ...]" = (1, 3, 5, 10)


class SecurityGateError(Exception):
    """Raised when a `security`-class invariant is treated as a tunable gate.

    Two triggers: (1) at import time, if a `security` row is declared with a
    loudness knob (a build error, AC5); (2) at query time, if a caller reads the
    ``loudness`` of a `security` output (there is no knob to read — it is pinned
    ``fail``, silenceable only by deleting its eval, US-102).
    """


@dataclass(frozen=True)
class GateClass:
    """One eval output's gate classification.

    ``category``     — the gate class: ``SECURITY`` or ``QUALITY`` (the AC's "class").
    ``determinism``  — ``DETERMINISTIC`` or ``NON_DETERMINISTIC`` (orthogonal, US-104).
    ``straddle``     — set to ``CEILING_IS_INVARIANT`` only for ``false_resolve``:
                       a quality metric whose ceiling breach is nonetheless pinned.
    ``loudness_default`` — the tunable default knob for a ``QUALITY`` output; MUST
                       be ``None`` for a ``SECURITY`` output (enforced at import).
    """

    name: str
    category: str
    determinism: str
    straddle: Optional[str] = None
    loudness_default: Optional[str] = None

    @property
    def is_security(self) -> bool:
        """True for a pinned-`fail` security invariant (E4/E6/AU4/E7-P1b)."""
        return self.category == SECURITY

    @property
    def is_quality(self) -> bool:
        """True for a tunable quality/regression metric."""
        return self.category == QUALITY

    @property
    def ceiling_pinned(self) -> bool:
        """True when a breach of this output's ceiling is a pinned hard fail
        regardless of any loudness knob (today only ``false_resolve``).

        This is the hook US-103/US-104 read: `false_resolve` is silenceable as a
        *metric* (a buyer may pick its ceiling value) but its ceiling breach can
        never be downgraded to a comment.
        """
        return self.straddle == CEILING_IS_INVARIANT

    @property
    def loudness(self) -> str:
        """The tunable loudness knob for this output.

        For a ``QUALITY`` output, returns its configured default (``comment`` when
        unset). For a ``SECURITY`` output there is **no knob** — it is pinned
        ``fail`` — so reading it RAISES ``SecurityGateError`` (the validation
        test's "querying its loudness knob raises"). Silence a security invariant
        only by deleting its eval (US-102).
        """
        if self.is_security:
            raise SecurityGateError(
                f"{self.name!r} is a security-class invariant, pinned `fail`; it "
                "carries no loudness knob and cannot be set to `off`/`comment`. "
                "Silence it only by deleting its eval/golden labels (US-102)."
            )
        return self.loudness_default or DEFAULT_LOUDNESS


def _validate_registry(rows: "list[GateClass]") -> "dict[str, GateClass]":
    """Validate the registry at import time and index it by output name.

    Enforces the invariants US-101 pins:
      * every name is unique (an eval output resolves to exactly one class);
      * every ``category`` / ``determinism`` / ``straddle`` is a known value;
      * no ``security`` row carries a loudness knob (AC5 build error, US-102);
      * a straddle marker only ever sits on a ``quality`` output.
    """
    registry: "dict[str, GateClass]" = {}
    for row in rows:
        if row.name in registry:
            raise ValueError(
                f"duplicate gate-class registration for {row.name!r}: every eval "
                "output must resolve to exactly one class"
            )
        if row.category not in (SECURITY, QUALITY):
            raise ValueError(
                f"{row.name!r} has unknown gate class {row.category!r} "
                f"(expected {SECURITY!r} or {QUALITY!r})"
            )
        if row.determinism not in (DETERMINISTIC, NON_DETERMINISTIC):
            raise ValueError(
                f"{row.name!r} has unknown determinism {row.determinism!r} "
                f"(expected {DETERMINISTIC!r} or {NON_DETERMINISTIC!r})"
            )
        if row.straddle is not None and row.straddle != CEILING_IS_INVARIANT:
            raise ValueError(
                f"{row.name!r} has unknown straddle marker {row.straddle!r} "
                f"(expected {CEILING_IS_INVARIANT!r})"
            )
        # AC5 / US-102: a security invariant is pinned `fail` — it must not carry
        # any loudness knob. A loudness setting on a security row is a build error.
        if row.category == SECURITY and row.loudness_default is not None:
            raise SecurityGateError(
                f"{row.name!r} is a security invariant and must not carry a "
                f"loudness knob (found {row.loudness_default!r}). Security gates "
                "are pinned `fail` and cannot be downgraded; delete the eval to "
                "remove it (US-102)."
            )
        if row.loudness_default is not None and row.loudness_default not in _LOUDNESS_VALUES:
            raise ValueError(
                f"{row.name!r} has unknown loudness {row.loudness_default!r} "
                f"(expected one of {sorted(_LOUDNESS_VALUES)})"
            )
        # A straddle only makes sense on a tunable (quality) metric — a security
        # output has no ceiling to straddle.
        if row.straddle is not None and row.category != QUALITY:
            raise ValueError(
                f"{row.name!r} carries a straddle marker but is not a quality "
                "output; only quality metrics can straddle a pinned ceiling"
            )
        registry[row.name] = row
    return registry


def _build_registry() -> "dict[str, GateClass]":
    """Enumerate every eval output and its gate classification (US-101 AC2/AC3/AC4)."""
    rows: "list[GateClass]" = []

    # -- SECURITY: pinned `fail` (US-102), deterministic binary asserts (US-105).
    #    E4  — the `security_no_access` table in runner.py::_aggregate_viewer_filter
    #          must read 1.0 / 0 gold for `no_access` under BOTH filter strategies.
    #    E6  — the cross-workspace boundary assertion (evals/retrieval/e6.py).
    #    AU4 — the API-layer auth-attack suite (backend/test_au4_auth_attacks.py).
    #    E7 P1b — customer-facing P1b output ≡ P1a: under a `no_access` viewer
    #          retrieval structurally returns 0 gold, so the pipeline defers to the
    #          fixed no-context (P1a) output. That equivalence is a deterministic
    #          assertion (it never depends on an LLM judge), so it is pinned
    #          `fail` AND deterministic — eligible to block a merge per-PR (US-105).
    rows += [
        GateClass("E4_zero_leak", SECURITY, DETERMINISTIC),
        GateClass("e6_workspace_boundary", SECURITY, DETERMINISTIC),
        GateClass("au4_auth_attacks", SECURITY, DETERMINISTIC),
        GateClass("e7_p1b_non_disclosure", SECURITY, DETERMINISTIC),
    ]

    # -- QUALITY: retrieval metrics — pure arithmetic over retrieved-vs-gold, so
    #    DETERMINISTIC (may block a merge per-PR, US-105). The k set mirrors
    #    runner.py::RECALL_KS (pinned locally to keep this module import-light; the
    #    test cross-checks it against the real constant when runner is importable).
    for k in _RECALL_KS:
        rows.append(GateClass(f"recall_at_{k}", QUALITY, DETERMINISTIC))
    rows.append(GateClass("mrr", QUALITY, DETERMINISTIC))
    rows.append(GateClass("ndcg_at_5", QUALITY, DETERMINISTIC))

    # -- QUALITY: the four RAGAS scores — LLM-judged, so NON_DETERMINISTIC
    #    (scheduled-only, never per-PR `fail`, US-105). Sourced from RAGAS_METRICS
    #    so the list can never drift from the metrics the suite emits.
    for metric in RAGAS_METRICS:
        rows.append(GateClass(metric, QUALITY, NON_DETERMINISTIC))

    # -- QUALITY: escalation rates — derived off the LLM-judged faithfulness leg
    #    (the "full E7 deflection/false-resolve sweep", US-105), so NON_DETERMINISTIC.
    #    `deflection_rate` / `false_escalate_rate` are plain tunable metrics; see
    #    e7_runner.py::E7Metrics.
    rows.append(GateClass("deflection_rate", QUALITY, NON_DETERMINISTIC))
    rows.append(GateClass("false_escalate_rate", QUALITY, NON_DETERMINISTIC))

    # -- `false_resolve` STRADDLES: a quality metric (the buyer sets its ceiling
    #    VALUE, US-050) whose ceiling breach is a pinned invariant (US-059). It is
    #    explicitly NOT a plain tunable metric — US-104's verdict layer treats a
    #    ceiling breach as a hard `fail` regardless of the escalation suite's
    #    loudness knob (`evals/gate/verdict.py`).
    rows.append(
        GateClass(
            "false_resolve",
            QUALITY,
            NON_DETERMINISTIC,
            straddle=CEILING_IS_INVARIANT,
        )
    )

    return _validate_registry(rows)


# The frozen registry, validated at import time. A malformed row (a security knob,
# an unknown class, a duplicate name) fails the import loudly.
_REGISTRY: "dict[str, GateClass]" = _build_registry()


def gate_class(name: str) -> GateClass:
    """Look up an eval output's gate classification.

    Raises ``KeyError`` for an unregistered name — an eval output must resolve to
    exactly one class, so an unknown name is a hard error, never a silent skip.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"{name!r} is not a registered eval output. Known outputs: "
            f"{', '.join(sorted(_REGISTRY))}"
        ) from None


def is_registered(name: str) -> bool:
    """True when ``name`` is a known eval output."""
    return name in _REGISTRY


def all_outputs() -> "tuple[str, ...]":
    """Every registered eval-output name, in registration order."""
    return tuple(_REGISTRY)


def security_outputs() -> "tuple[str, ...]":
    """The pinned-`fail` security invariants (E4/E6/AU4/E7-P1b)."""
    return tuple(n for n, gc in _REGISTRY.items() if gc.category == SECURITY)


def quality_outputs() -> "tuple[str, ...]":
    """The tunable quality/regression metrics (incl. the straddling `false_resolve`)."""
    return tuple(n for n, gc in _REGISTRY.items() if gc.category == QUALITY)


def registry() -> "dict[str, GateClass]":
    """A shallow copy of the full registry, keyed by output name.

    Copied so a caller can iterate/mutate their view without disturbing the pinned
    module-level registry (``GateClass`` itself is frozen).
    """
    return dict(_REGISTRY)
