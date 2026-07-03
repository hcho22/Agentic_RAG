"""US-102 tests: pin the security/correctness gate to `fail`.

Two layers (mirrors ``test_classes.py`` / the repo's other eval tests):

1. **Offline unit checks that ALWAYS run** — no DB, no secrets, no heavy deps
   (only stdlib + PyYAML + the import-light ``evals.gate`` package). They encode
   the US-102 validation test directly:
     * a gate declaration that tries ``E4_zero_leak: off`` (or
       ``e6_workspace_boundary: comment``) FAILS to load with the actionable
       pinned-`fail` message — from a dict AND from a real YAML file;
     * every security member (E4/E6/AU4/E7-P1b) is equally un-downgradable;
     * a quality verdict loads fine and resolves (explicit + registry default);
     * the E4 zero-leak binary assert flags a leaking ``no_access`` cell (< 1.0)
       and passes a clean (all-1.0) table, independent of any verdict config.

2. **Best-effort integration guard** — when the heavy ``runner`` module is
   importable, drive a leaking ``no_access`` fixture through the REAL
   ``_aggregate_viewer_filter`` and assert the produced ``security_no_access``
   table trips the binary assert (proving the runner's exit-path input, not a
   hand-built table). Skips cleanly (no failure) when runner's deps
   (asyncpg / openai / httpx) are absent.

Run:
    python -m evals.gate.test_pinned_security
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import Tuple, Type, Union

from evals.gate.classes import (
    SecurityGateError,
    gate_class,
    security_outputs,
)
from evals.gate.declaration import (
    _PINNED_MESSAGE,
    GateDeclaration,
    load_gate_declaration,
)
from evals.gate.security import (
    E4_ZERO_LEAK,
    SecurityViolation,
    assert_no_access_zero_leak,
    check_no_access_zero_leak,
    e4_structurally_blind,
    no_access_exercised,
)


_ExcSpec = Union[Type[BaseException], Tuple[Type[BaseException], ...]]


def _assert_raises(exc_type: _ExcSpec, fn, *, what: str) -> BaseException:
    try:
        fn()
    except exc_type as exc:
        return exc
    names = getattr(exc_type, "__name__", str(exc_type))
    raise AssertionError(f"expected {names} from {what}, none raised")


# ---------------------------------------------------------------------------
# Layer 1 — the US-102 validation test (always runs)
# ---------------------------------------------------------------------------


def _loader_rejects_security_downgrade() -> None:
    """AC1/AC5 + validation steps 1 & 3: a verdict on a security output fails to
    load with the pinned-`fail` message; the roster is rejected wholesale."""

    # The two declarations the PRD validation test authors verbatim.
    for decl in ({"verdicts": {"E4_zero_leak": "off"}},
                 {"verdicts": {"e6_workspace_boundary": "comment"}}):
        exc = _assert_raises(
            SecurityGateError,
            lambda decl=decl: load_gate_declaration(decl),
            what=f"loading a declaration that downgrades a security output: {decl}",
        )
        msg = str(exc)
        # The exact, audit-quotable phrasing a security reviewer greps for.
        assert _PINNED_MESSAGE in msg, msg
        assert "delete the eval" in msg, msg

    # EVERY security invariant (E4/E6/AU4/E7-P1b) is equally un-downgradable, for
    # each of the three loudness values a buyer might try.
    for name in security_outputs():
        for verdict in ("off", "comment", "fail"):
            _assert_raises(
                SecurityGateError,
                lambda name=name, verdict=verdict: load_gate_declaration(
                    {"verdicts": {name: verdict}}
                ),
                what=f"downgrading {name} to {verdict}",
            )

    # The security roster the loader pins matches US-101's registry exactly.
    assert set(security_outputs()) == {
        "E4_zero_leak",
        "e6_workspace_boundary",
        "au4_auth_attacks",
        "e7_p1b_non_disclosure",
    }, security_outputs()


def _loader_rejects_from_a_real_yaml_file() -> None:
    """AC5: a gate-declaration *YAML file* that tries ``E4_zero_leak: comment``
    fails to load with an actionable error (not just a dict fixture)."""
    yaml_text = "verdicts:\n  E4_zero_leak: comment\n"
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(yaml_text)
        path = fh.name
    try:
        exc = _assert_raises(
            SecurityGateError,
            lambda: load_gate_declaration(path),
            what="loading a YAML file that downgrades E4_zero_leak",
        )
        assert _PINNED_MESSAGE in str(exc), str(exc)
    finally:
        os.unlink(path)


def _yaml_boolean_off_is_normalized() -> None:
    """YAML 1.1 gotcha: an unquoted `off` verdict parses to the boolean `False`.
    `off` is a valid verdict, so it must normalize to "off" from a real YAML file
    (quoted or unquoted) — and a security output written `off` is still rejected
    with the intended verdict shown, while `on`/`true` (-> True) stays invalid."""

    def _write(text: str) -> str:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(text)
            return fh.name

    # A quality output written `off` UNQUOTED loads as the "off" verdict (the bug
    # the no-mistakes test agent surfaced: without normalization this errored
    # because YAML `off` -> False is not the string "off").
    p_unquoted = _write("verdicts:\n  recall_at_5: off\n")
    p_quoted = _write('verdicts:\n  recall_at_5: "off"\n')
    try:
        assert load_gate_declaration(p_unquoted).verdict_for("recall_at_5") == "off"
        assert load_gate_declaration(p_quoted).verdict_for("recall_at_5") == "off"
    finally:
        os.unlink(p_unquoted)
        os.unlink(p_quoted)

    # A dict `False` (however produced) is normalized too.
    assert load_gate_declaration(
        {"verdicts": {"recall_at_5": False}}
    ).verdict_for("recall_at_5") == "off"

    # A SECURITY output written `off` unquoted is still rejected (the pin holds),
    # and the message shows the normalized `'off'`, not `False`.
    p_sec = _write("verdicts:\n  E4_zero_leak: off\n")
    try:
        exc = _assert_raises(
            SecurityGateError,
            lambda: load_gate_declaration(p_sec),
            what="a security output set to unquoted-off in YAML",
        )
        assert "'off'" in str(exc), str(exc)
        assert _PINNED_MESSAGE in str(exc), str(exc)
    finally:
        os.unlink(p_sec)

    # A truthy YAML boolean (`on`/`yes`/`true` -> True) is NOT a verdict — still a
    # hard error, now with a helpful hint about the gotcha.
    exc = _assert_raises(
        ValueError,
        lambda: load_gate_declaration({"verdicts": {"recall_at_5": True}}),
        what="a truthy YAML boolean verdict",
    )
    assert "YAML" in str(exc), str(exc)


def _loader_accepts_quality_and_resolves() -> None:
    """A quality verdict map loads and resolves — explicit settings win, unset
    quality outputs fall back to the registry default (`comment`)."""
    decl = load_gate_declaration(
        {"verdicts": {"recall_at_5": "fail", "deflection_rate": "off"}}
    )
    assert isinstance(decl, GateDeclaration)
    assert decl.verdict_for("recall_at_5") == "fail"
    assert decl.verdict_for("deflection_rate") == "off"
    # An unset quality output resolves to the registry default.
    assert decl.verdict_for("mrr") == "comment"
    assert decl.verdict_for("ndcg_at_5") == "comment"

    # `false_resolve` is a quality metric — its *loudness* knob is tunable here
    # (its ceiling breach is pinned elsewhere, US-103/104); it loads fine.
    fr = load_gate_declaration({"verdicts": {"false_resolve": "comment"}})
    assert fr.verdict_for("false_resolve") == "comment"

    # Reading a security output's verdict through a loaded declaration RE-PINS it
    # (mirrors GateClass.loudness): there is no knob to read.
    _assert_raises(
        SecurityGateError,
        lambda: decl.verdict_for("E4_zero_leak"),
        what="reading a security output's verdict off a declaration",
    )

    # An empty declaration is a valid no-op (all registry defaults).
    empty = load_gate_declaration({})
    assert empty.verdict_for("recall_at_5") == "comment"


def _loader_rejects_malformed_declarations() -> None:
    """Hard errors (no silent skip): unknown output, unknown section, invalid
    verdict value, non-mapping shapes."""
    cases = {
        "unknown output": {"verdicts": {"totally_made_up": "off"}},
        "unknown section": {"thresholds": {"x": 1}},
        "invalid verdict value": {"verdicts": {"recall_at_5": "loud"}},
        "non-mapping verdicts": {"verdicts": ["recall_at_5"]},
    }
    for label, decl in cases.items():
        _assert_raises(
            (ValueError, SecurityGateError),
            lambda decl=decl: load_gate_declaration(decl),
            what=f"loading a malformed declaration ({label})",
        )

    # A non-mapping top-level YAML document is rejected.
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write("- just\n- a\n- list\n")
        path = fh.name
    try:
        _assert_raises(
            ValueError,
            lambda: load_gate_declaration(path),
            what="loading a top-level-list YAML declaration",
        )
    finally:
        os.unlink(path)


def _e4_binary_assert() -> None:
    """AC2/AC3 + validation step 2: the E4 zero-leak invariant is a binary assert
    (`== 1.0` per cell), tripping non-zero on a leak, clean otherwise."""

    # E4 is a registered security invariant (cross-link to US-101's registry).
    assert E4_ZERO_LEAK in security_outputs()
    assert gate_class(E4_ZERO_LEAK).is_security

    clean = {
        "pre_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 1.0},
        "post_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 1.0},
    }
    assert check_no_access_zero_leak(clean) == [], "a clean table must not fault"
    assert no_access_exercised(clean) is True
    assert_no_access_zero_leak(clean)  # must NOT raise

    # A fixture where `no_access` leaked one gold chunk on one cell (< 1.0).
    leak = {
        "pre_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 0.98},
        "post_filter": {"vector": 1.0, "keyword": 1.0, "hybrid": 1.0},
    }
    violations = check_no_access_zero_leak(leak)
    assert len(violations) == 1, violations
    v = violations[0]
    assert isinstance(v, SecurityViolation)
    assert v.output == E4_ZERO_LEAK and v.filter == "pre_filter" and v.mode == "hybrid"
    assert v.observed == 0.98
    # A breach RAISES (fail non-zero) — it can never be downgraded to a warning.
    exc = _assert_raises(
        SecurityGateError,
        lambda: assert_no_access_zero_leak(leak),
        what="asserting a leaking security_no_access table",
    )
    assert "cannot be downgraded" in str(exc), str(exc)

    # The invariant only asserts where it runs: an empty table (no_access viewer
    # not part of the run) is "not exercised", never a false pass or fail.
    assert check_no_access_zero_leak({}) == []
    assert no_access_exercised({}) is False
    assert no_access_exercised({"pre_filter": {}, "post_filter": {}}) is False


def _e4_structural_blindness_guard() -> None:
    """The runner's fail-closed blindness guard: when the ``no_access`` viewer was
    REQUESTED but its sweep produced 0 cells, the pinned E4 gate must NOT pass
    vacuously (mirrors E6's structural-blindness guard, US-102). When ``no_access``
    was not requested the invariant simply does not apply — a skip, not a fail."""

    empty_tables: tuple[dict, ...] = ({}, {"pre_filter": {}, "post_filter": {}})
    exercised: dict = {
        "pre_filter": {"vector": 1.0, "hybrid": 1.0},
        "post_filter": {"vector": 1.0, "hybrid": 1.0},
    }
    requested: tuple[tuple[str, ...], ...] = (
        ("full_access", "partial_access", "no_access"),
        ("no_access",),
    )
    not_requested: tuple[tuple[str, ...], ...] = (
        ("full_access",),
        ("full_access", "partial_access"),
        (),
    )

    # REQUESTED but not exercised → structurally blind (fail non-zero in amain).
    for viewers in requested:
        for table in empty_tables:
            assert e4_structurally_blind(viewers, table) is True, (viewers, table)

    # REQUESTED and exercised → not blind (the real leak check then decides).
    assert e4_structurally_blind(("no_access",), exercised) is False

    # NOT requested → the invariant does not apply, so never blind — even when the
    # table is empty (`--viewers full` legitimately produces no no_access cells).
    for viewers in not_requested:
        for table in empty_tables + (exercised,):
            assert e4_structurally_blind(viewers, table) is False, (viewers, table)


# ---------------------------------------------------------------------------
# Layer 2 — best-effort integration guard (skips without the heavy runner)
# ---------------------------------------------------------------------------


def _real_aggregation_feeds_the_assert() -> None:
    """When runner is importable, prove the REAL `_aggregate_viewer_filter`
    produces a sub-1.0 `security_no_access` cell for a leaking `no_access` run,
    which the binary assert then trips (the runner's exit-path input)."""
    try:
        from evals.retrieval.runner import _aggregate_viewer_filter  # heavy deps
    except Exception as exc:  # ImportError / missing-dep chain
        print(
            f"integration guard: runner not importable ({type(exc).__name__}) "
            "— skipping real-aggregation check"
        )
        return

    def _block(recall_at_10: float) -> "dict[str, float]":
        return {
            "recall_at_5": recall_at_10,
            "mrr": recall_at_10,
            "ndcg_at_5": recall_at_10,
            "recall_at_10": recall_at_10,
        }

    def _q(recall_at_10: float) -> "dict[str, object]":
        return {
            "category": "policy",
            "by_viewer": {
                "no_access": {
                    "hybrid": {
                        "pre_filter": _block(recall_at_10),
                        "post_filter": _block(0.0),
                    }
                }
            },
        }

    # Two no_access questions; the second LEAKED gold on the pre-filter leg.
    per_question = [_q(0.0), _q(1.0)]
    agg = _aggregate_viewer_filter(per_question, ("hybrid",))
    table = agg["security_no_access"]
    # 1 of 2 runs returned zero gold on pre_filter/hybrid → 0.5 (a leak).
    assert table["pre_filter"]["hybrid"] == 0.5, table
    assert table["post_filter"]["hybrid"] == 1.0, table
    violations = check_no_access_zero_leak(table)
    assert any(
        v.filter == "pre_filter" and v.mode == "hybrid" for v in violations
    ), violations
    _assert_raises(
        SecurityGateError,
        lambda: assert_no_access_zero_leak(table),
        what="asserting a runner-produced leaking table",
    )

    # A clean run (no leak) produces the 1.000 table and passes.
    clean_agg = _aggregate_viewer_filter([_q(0.0), _q(0.0)], ("hybrid",))
    clean_table = clean_agg["security_no_access"]
    assert clean_table["pre_filter"]["hybrid"] == 1.0, clean_table
    assert check_no_access_zero_leak(clean_table) == []
    print("integration guard OK: real aggregation of a leak trips the E4 assert")


def main() -> None:
    _loader_rejects_security_downgrade()
    _loader_rejects_from_a_real_yaml_file()
    _yaml_boolean_off_is_normalized()
    _loader_accepts_quality_and_resolves()
    _loader_rejects_malformed_declarations()
    _e4_binary_assert()
    _e4_structural_blindness_guard()
    _real_aggregation_feeds_the_assert()
    print(
        "US-102 pinned-security gate OK: "
        f"{len(security_outputs())} security invariants un-downgradable; "
        "E4 zero-leak binary assert live"
    )


if __name__ == "__main__":
    # Allow `python -m evals.gate.test_pinned_security` from the repo root.
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    main()
