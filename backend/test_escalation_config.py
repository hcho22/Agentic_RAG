"""US-050 validation test: escalation config — three global knobs + ceiling.

Exercises `escalation.EscalationConfig.from_env` and
`escalation.get_false_resolve_ceiling` directly — pure env parsing/validation,
no LLM, no DB/network/secrets — so it runs anywhere, like
`test_chat_mode_default.py` / `test_escalation_gate.py`.

Covers the PRD validation test:
  * valid `ESCALATION_TAU_SIM=0.4` / `ESCALATION_N_MIN=2` / cutoff `0.7` load and
    are exactly reflected on the frozen config object;
  * `ESCALATION_TAU_SIM=1.5` (and other out-of-range / non-numeric knobs) raises
    a clear `ValueError` naming the env var (the "fail at startup" requirement);
  * omitting all knobs yields the documented ADR-0003 / E7-derived defaults;
  * the gate actually CONSUMES the config knobs (a strong-vs-weak flip driven by
    `tau_sim` alone) — proving the knob is config-driven, not hardcoded in the
    gate (the PRD failure indicator);
  * the false-resolve ceiling is a SEPARATE value and is NOT a field on
    `EscalationConfig` — structurally kept off the per-request path (the other
    PRD failure indicator);
plus frozen-immutability and direct-construction validation (defense in depth).

Run:
    python -m backend.test_escalation_config
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from pydantic import ValidationError  # noqa: E402

from escalation import (  # noqa: E402
    DEFAULT_FAITHFULNESS_CUTOFF,
    DEFAULT_FALSE_RESOLVE_CEILING,
    DEFAULT_N_MIN,
    DEFAULT_TAU_SIM,
    EscalationConfig,
    get_false_resolve_ceiling,
    retrieval_gate,
)
from retrieval import SearchDocumentsResult  # noqa: E402

# Every escalation env knob, so each test starts from a known-clean slate
# regardless of the ambient environment or test ordering.
_ESC_VARS = (
    "ESCALATION_TAU_SIM",
    "ESCALATION_N_MIN",
    "ESCALATION_FAITHFULNESS_CUTOFF",
    "ESCALATION_FALSE_RESOLVE_CEILING",
)


@contextmanager
def env(**overrides: str) -> Iterator[None]:
    """Run a block with the escalation env knobs set exactly to `overrides`.

    All four knobs are cleared first (so an unspecified knob is genuinely unset,
    exercising the default path), the overrides applied, and the prior
    environment restored afterwards — no cross-test leakage.
    """
    saved = {k: os.environ.get(k) for k in _ESC_VARS}
    try:
        for k in _ESC_VARS:
            os.environ.pop(k, None)
        for k, v in overrides.items():
            os.environ[k] = v
        yield
    finally:
        for k, prior in saved.items():
            if prior is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prior


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _result(cosine: float) -> SearchDocumentsResult:
    """A minimal SearchDocumentsResult carrying a given raw cosine."""
    return SearchDocumentsResult(
        id=f"c{cosine}",
        document_id="d1",
        chunk_index=0,
        content="x",
        similarity=cosine,
        filename="f.txt",
        cosine_similarity=cosine,
    )


def test_valid_values_load() -> None:
    """PRD: valid knobs load and are reflected exactly on the frozen config."""
    with env(
        ESCALATION_TAU_SIM="0.4",
        ESCALATION_N_MIN="2",
        ESCALATION_FAITHFULNESS_CUTOFF="0.7",
        ESCALATION_FALSE_RESOLVE_CEILING="0.05",
    ):
        cfg = EscalationConfig.from_env()
        _check(cfg.tau_sim == 0.4, f"tau_sim should be 0.4, got {cfg.tau_sim}")
        _check(cfg.n_min == 2, f"n_min should be 2, got {cfg.n_min}")
        _check(
            cfg.faithfulness_cutoff == 0.7,
            f"cutoff should be 0.7, got {cfg.faithfulness_cutoff}",
        )
        _check(
            get_false_resolve_ceiling() == 0.05,
            f"ceiling should be 0.05, got {get_false_resolve_ceiling()}",
        )
    print("ok: valid ESCALATION_* knobs load and are reflected on the config")


def test_defaults_when_unset() -> None:
    """PRD: omitting all knobs yields the documented ADR-0003/E7 defaults."""
    with env():
        cfg = EscalationConfig.from_env()
        _check(cfg.tau_sim == DEFAULT_TAU_SIM, "tau_sim should default")
        _check(cfg.n_min == DEFAULT_N_MIN, "n_min should default")
        _check(
            cfg.faithfulness_cutoff == DEFAULT_FAITHFULNESS_CUTOFF,
            "cutoff should default",
        )
        _check(
            get_false_resolve_ceiling() == DEFAULT_FALSE_RESOLVE_CEILING,
            "ceiling should default",
        )
    print("ok: all knobs unset -> documented defaults")


def test_blank_and_whitespace_use_default() -> None:
    """Blank / whitespace-only values are treated as unset (mirrors the
    `get_similarity_threshold` parsing), not parsed as 0."""
    with env(
        ESCALATION_TAU_SIM="",
        ESCALATION_N_MIN="   ",
        ESCALATION_FAITHFULNESS_CUTOFF=" ",
        ESCALATION_FALSE_RESOLVE_CEILING="",
    ):
        cfg = EscalationConfig.from_env()
        _check(cfg.tau_sim == DEFAULT_TAU_SIM, "blank tau_sim -> default")
        _check(cfg.n_min == DEFAULT_N_MIN, "whitespace n_min -> default")
        _check(
            cfg.faithfulness_cutoff == DEFAULT_FAITHFULNESS_CUTOFF,
            "whitespace cutoff -> default",
        )
        _check(
            get_false_resolve_ceiling() == DEFAULT_FALSE_RESOLVE_CEILING,
            "blank ceiling -> default",
        )
    print("ok: blank/whitespace knobs are treated as unset (default), not 0")


def test_tau_sim_out_of_range_raises() -> None:
    """PRD core case: ESCALATION_TAU_SIM=1.5 raises a clear ValueError that names
    the env var (a misconfiguration must fail the boot, never silently clamp)."""
    for bad in ("1.5", "-0.1"):
        with env(ESCALATION_TAU_SIM=bad):
            try:
                EscalationConfig.from_env()
            except ValueError as e:
                _check(
                    "ESCALATION_TAU_SIM" in str(e),
                    f"error must name the env var, got: {e!r}",
                )
                continue
            raise AssertionError(f"ESCALATION_TAU_SIM={bad} must raise ValueError")
    print("ok: out-of-range ESCALATION_TAU_SIM fails closed (ValueError naming the var)")


def test_cutoff_and_ceiling_out_of_range_raise() -> None:
    """The other two [0,1] knobs validate identically and name their own var."""
    with env(ESCALATION_FAITHFULNESS_CUTOFF="1.2"):
        try:
            EscalationConfig.from_env()
        except ValueError as e:
            _check("ESCALATION_FAITHFULNESS_CUTOFF" in str(e), f"got: {e!r}")
        else:
            raise AssertionError("cutoff=1.2 must raise ValueError")
    with env(ESCALATION_FALSE_RESOLVE_CEILING="2"):
        try:
            get_false_resolve_ceiling()
        except ValueError as e:
            _check("ESCALATION_FALSE_RESOLVE_CEILING" in str(e), f"got: {e!r}")
        else:
            raise AssertionError("ceiling=2 must raise ValueError")
    print("ok: out-of-range cutoff and ceiling each fail closed naming their var")


def test_n_min_below_one_raises() -> None:
    """N is a count: it must be >= 1. N=0 (and negatives) fail closed."""
    for bad in ("0", "-3"):
        with env(ESCALATION_N_MIN=bad):
            try:
                EscalationConfig.from_env()
            except ValueError as e:
                _check("ESCALATION_N_MIN" in str(e), f"error must name the var: {e!r}")
                _check(">= 1" in str(e), f"error should state the >=1 bound: {e!r}")
                continue
            raise AssertionError(f"ESCALATION_N_MIN={bad} must raise ValueError")
    print("ok: ESCALATION_N_MIN < 1 fails closed (ValueError, >=1 bound stated)")


def test_non_numeric_raises() -> None:
    """A non-numeric knob raises (never silently ignored / coerced)."""
    with env(ESCALATION_TAU_SIM="high"):
        try:
            EscalationConfig.from_env()
        except ValueError as e:
            _check("ESCALATION_TAU_SIM" in str(e), f"got: {e!r}")
        else:
            raise AssertionError("non-float tau_sim must raise ValueError")
    with env(ESCALATION_N_MIN="2.5"):
        try:
            EscalationConfig.from_env()
        except ValueError as e:
            _check("ESCALATION_N_MIN" in str(e), f"got: {e!r}")
        else:
            raise AssertionError("non-int n_min must raise ValueError")
    print("ok: non-numeric tau_sim / n_min fail closed (ValueError)")


def test_config_drives_the_gate() -> None:
    """PRD failure indicator: the gate must READ the config knobs, not hardcode
    them. With a fixed result set (top1 cosine 0.5, 2 rows clearing 0.3), the
    SAME inputs flip strong<->weak purely on `tau_sim` sourced from config."""
    results = [_result(0.5), _result(0.4)]  # top1=0.5, both clear match_threshold=0.3
    with env(ESCALATION_TAU_SIM="0.4", ESCALATION_N_MIN="2"):
        cfg = EscalationConfig.from_env()
        strong = retrieval_gate(results, cfg.tau_sim, cfg.n_min, match_threshold=0.3)
        _check(strong.strong, "tau_sim=0.4 <= top1=0.5 should be strong")
    with env(ESCALATION_TAU_SIM="0.6", ESCALATION_N_MIN="2"):
        cfg = EscalationConfig.from_env()
        weak = retrieval_gate(results, cfg.tau_sim, cfg.n_min, match_threshold=0.3)
        _check(not weak.strong, "tau_sim=0.6 > top1=0.5 should be weak")
    print("ok: the gate consumes the config tau_sim/n_min (not hardcoded)")


def test_ceiling_is_not_a_config_field() -> None:
    """PRD failure indicator: the false-resolve ceiling must stay OFF the
    per-request path. Assert it is neither a field nor an attribute of the config
    object the pipeline reads — structurally un-wireable into the latency path."""
    fields = set(EscalationConfig.model_fields)
    _check(
        fields == {"tau_sim", "n_min", "faithfulness_cutoff"},
        f"EscalationConfig must carry only the 3 gate knobs, got {sorted(fields)}",
    )
    for forbidden in ("false_resolve_ceiling", "ceiling", "false_resolve"):
        _check(
            forbidden not in fields,
            f"{forbidden!r} must NOT be a field on EscalationConfig",
        )
    with env():
        cfg = EscalationConfig.from_env()
        _check(
            not hasattr(cfg, "false_resolve_ceiling"),
            "config object must not expose the ceiling as an attribute",
        )
    print("ok: false-resolve ceiling is separate — not a field/attr on the config")


def test_frozen_and_direct_construction_validates() -> None:
    """The config is frozen (immutable) and self-validates on direct
    construction (defense in depth: not only the env path range-checks)."""
    cfg = EscalationConfig(tau_sim=0.4, n_min=2, faithfulness_cutoff=0.7)
    try:
        cfg.tau_sim = 0.9  # type: ignore[misc]
    except ValidationError:
        pass
    else:
        raise AssertionError("EscalationConfig must be frozen (immutable)")
    for kwargs in (
        {"tau_sim": 1.5, "n_min": 2, "faithfulness_cutoff": 0.7},
        {"tau_sim": 0.4, "n_min": 0, "faithfulness_cutoff": 0.7},
        {"tau_sim": 0.4, "n_min": 2, "faithfulness_cutoff": -0.1},
    ):
        try:
            EscalationConfig(**kwargs)  # type: ignore[arg-type]
        except ValidationError:
            continue
        raise AssertionError(f"direct construction with {kwargs} must fail validation")
    print("ok: config is frozen and self-validating on direct construction")


def main() -> int:
    tests = [
        test_valid_values_load,
        test_defaults_when_unset,
        test_blank_and_whitespace_use_default,
        test_tau_sim_out_of_range_raises,
        test_cutoff_and_ceiling_out_of_range_raise,
        test_n_min_below_one_raises,
        test_non_numeric_raises,
        test_config_drives_the_gate,
        test_ceiling_is_not_a_config_field,
        test_frozen_and_direct_construction_validates,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} escalation-config (US-050) test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
