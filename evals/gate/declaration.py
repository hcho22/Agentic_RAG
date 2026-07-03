"""US-102 (Epic F, ADR-0005): pin the security/correctness gate to `fail`.

This is the **gate-declaration loader**. A buyer authors a declaration (a YAML
file that ships beside their golden set, US-103) whose ``verdicts:`` block sets a
per-output loudness knob (``off | comment | fail``, US-104) over the **quality**
metrics. This loader enforces the single US-102 invariant:

    A ``security``-class output (E4 / E6 / AU4 / E7-P1b) can NEVER appear in that
    verdict map. Security invariants are pinned ``fail`` and are simply *not
    present in the tunable verdict space* — the only way to stop one is to
    **delete** its eval/golden labels (a loud, tracked diff, US-110), never a
    quiet config flag.

So a declaration that tries ``E4_zero_leak: off`` (or ``e6_workspace_boundary:
comment``) is a **hard load error** with an actionable message, not a silent
downgrade. This is what makes "the security gates cannot be turned off, and here
is the eval that proves it" a true statement a buyer's security reviewer can
audit.

Scope discipline (this is US-102, not US-103/104/105):

* This loader validates ONLY the ``verdicts:`` section — the tunable loudness map.
  US-103 extends the schema with the detection *bindings* (cells, judge map,
  threshold constants) as additional top-level sections; US-104 adds the
  ``(severity, knob) -> action`` verdict function that consumes ``verdicts``;
  US-105 adds the per-PR-vs-scheduled determinism check. Each extends
  ``_KNOWN_SECTIONS`` / this dataclass; none of them may relax the security pin
  below.
* A verdict key is a registered **eval-output name** (matching US-102's own
  validation test, which authors ``E4_zero_leak: off``). US-104 layers the
  per-suite grouping on top of this per-output map; a security-output key stays
  rejected regardless of granularity, because a security output is in no suite.

The class name for the loudness value is the AC's ``off | comment | fail`` verdict
(spelled ``loudness`` in :mod:`evals.gate.classes`, off the reserved ``class``
keyword there).

Run the tests: ``python -m evals.gate.test_pinned_security``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Union

from evals.gate.classes import (
    _LOUDNESS_VALUES,
    SecurityGateError,
    gate_class,
    is_registered,
)

# The one top-level section US-102 owns. Later stories extend this set:
#   US-103 adds the detection bindings (`bindings:` / cell + threshold config),
#   so a declaration with only those sections still loads here. Kept as a frozen
#   set so an unknown top-level key is a loud typo, not a silent skip.
_KNOWN_SECTIONS = frozenset({"verdicts"})

# The exact, audit-quotable message a downgrade attempt produces. Security
# reviewers grep for this; keep the wording stable.
_PINNED_MESSAGE = (
    "security gates are pinned `fail` and cannot be downgraded; "
    "delete the eval to remove it"
)


@dataclass(frozen=True)
class GateDeclaration:
    """A loaded, validated gate declaration.

    ``verdicts`` holds ONLY the buyer's explicit per-output loudness settings for
    **quality** outputs (a security output can never appear — the loader rejects
    it). It is intentionally the explicit map, not a fully-materialized one; read
    a resolved value (with the registry default filled in) through
    :meth:`verdict_for`, which also re-pins security outputs so a caller can never
    read a loudness for one.
    """

    verdicts: Mapping[str, str]

    def verdict_for(self, output: str) -> str:
        """The resolved loudness verdict for ``output``.

        Returns the buyer's explicit setting when present, else the registry
        default (``comment``). Raises :class:`SecurityGateError` for a
        ``security``-class output — there is no knob to read, it is pinned
        ``fail`` (mirrors ``GateClass.loudness``). Raises ``KeyError`` for an
        unregistered output.
        """
        gc = gate_class(output)  # KeyError for an unknown output (no silent skip)
        if gc.is_security:
            raise SecurityGateError(
                f"{output!r} is a security-class invariant, pinned `fail`; "
                f"{_PINNED_MESSAGE} (US-102)."
            )
        if output in self.verdicts:
            return self.verdicts[output]
        return gc.loudness  # the registry default for a quality output


def _coerce_source(source: "Union[str, Path, Mapping[str, Any]]") -> "Mapping[str, Any]":
    """Read the declaration into a plain mapping.

    Accepts a path (``str``/``Path`` to a YAML file) or an already-parsed mapping
    (handy for tests). A path is parsed with ``yaml.safe_load``; a non-mapping
    document (e.g. a bare list, or an empty file) is a hard error.
    """
    if isinstance(source, Mapping):
        return source
    path = Path(source)
    import yaml  # type: ignore[import-untyped]  # local import: keep module import-light

    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        # An empty declaration is a valid no-op (all defaults), not an error.
        return {}
    if not isinstance(data, Mapping):
        raise ValueError(
            f"gate declaration {str(path)!r} must be a mapping at the top level, "
            f"got {type(data).__name__}"
        )
    return data


def load_gate_declaration(
    source: "Union[str, Path, Mapping[str, Any]]",
) -> GateDeclaration:
    """Load and validate a gate declaration; return a :class:`GateDeclaration`.

    Enforces the US-102 invariants (each a hard error, non-zero exit for a CLI
    caller — no silent skip / silent downgrade):

    * an unknown top-level section is rejected (a typo is loud);
    * ``verdicts`` must be a mapping of ``output -> verdict``;
    * every verdict key must be a **registered** eval output (unknown → error);
    * a **security**-class key is rejected with the pinned-``fail`` message —
      the load-time enforcement of "silence only by deletion" (AC1/AC5);
    * every verdict value must be one of ``off | comment | fail`` — an unquoted
      YAML ``off`` (which parses to the boolean ``False``) is normalized back to
      ``"off"`` so a buyer's natural ``recall_at_5: off`` works unquoted.
    """
    data = _coerce_source(source)

    unknown_sections = set(data) - _KNOWN_SECTIONS
    if unknown_sections:
        raise ValueError(
            f"unknown gate-declaration section(s) {sorted(unknown_sections)}; "
            f"US-102 knows {sorted(_KNOWN_SECTIONS)} (later stories add more). "
            "A stray/misspelled section is a hard error, never a silent skip."
        )

    raw_verdicts = data.get("verdicts")
    if raw_verdicts is None:
        raw_verdicts = {}
    if not isinstance(raw_verdicts, Mapping):
        raise ValueError(
            "gate-declaration `verdicts` must be a mapping of "
            f"output -> ({'|'.join(sorted(_LOUDNESS_VALUES))}); got "
            f"{type(raw_verdicts).__name__}"
        )

    verdicts: dict[str, str] = {}
    for name, verdict in raw_verdicts.items():
        name = str(name)

        # (0) YAML 1.1 boolean gotcha: an UNQUOTED `off` verdict parses to the
        #     boolean False (as `on`/`yes`/`true` parse to True), never the
        #     string "off". `off` IS a valid verdict, so normalize False -> "off"
        #     up front — a buyer's natural `recall_at_5: off` then works whether
        #     or not they quote it, and the security-rejection message below reads
        #     `'off'` rather than `False`. A truthy YAML boolean (`on`/`yes`/
        #     `true`) is not a verdict and still falls through to the invalid-value
        #     error at (3).
        if verdict is False:
            verdict = "off"

        # (1) The load-time security pin — the heart of US-102. A security output
        #     has no tunable verdict; setting one is a build error, not a downgrade.
        if is_registered(name) and gate_class(name).is_security:
            raise SecurityGateError(
                f"gate declaration tries to set {name!r} to {verdict!r}: "
                f"{_PINNED_MESSAGE} (US-102). Security invariants "
                "(E4/E6/AU4/E7-P1b) are pinned `fail` and are not present in the "
                "tunable verdict map."
            )

        # (2) Unknown output — hard error, no silent skip (mirrors US-101's
        #     `gate_class` and US-103's unknown-cell rule).
        if not is_registered(name):
            raise ValueError(
                f"gate declaration sets a verdict on unknown eval output {name!r}. "
                "Every verdict key must be a registered quality output "
                "(security invariants carry no knob; see US-101/US-102)."
            )

        # (3) Invalid verdict value. A leftover YAML boolean here is a truthy
        #     `on`/`yes`/`true` (False was normalized to "off" above), so hint at
        #     the gotcha rather than emit a bare `True`.
        if not isinstance(verdict, str) or verdict not in _LOUDNESS_VALUES:
            hint = ""
            if isinstance(verdict, bool):
                hint = (
                    " (an unquoted YAML `on`/`yes`/`true` parses to a boolean; "
                    "only `off|comment|fail` are verdicts)"
                )
            raise ValueError(
                f"gate declaration sets {name!r} to invalid verdict {verdict!r}; "
                f"expected one of {sorted(_LOUDNESS_VALUES)}{hint}"
            )

        verdicts[name] = verdict

    return GateDeclaration(verdicts=verdicts)
