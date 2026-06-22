"""US-027 validation test: the fail-closed embedder-drift startup guard.

Exercises `embeddings.check_embedder_drift` directly — the pure decision the
`main.py` startup hook calls once, after probe-embedding one string to measure
the live embedder's ACTUAL output dim and reading the US-026 corpus stamp. The
function is I/O-free (the caller does the probe-embed + stamp read), so this
test constructs the (configured_model, measured_dim, stamp) inputs inline and
needs no DB / network / secrets — it runs anywhere, like `test_chat_mode_default.py`.

Covers the PRD validation test (assert-style fail-closed guard):
  * stamp (text-embedding-3-small, 1536) + configured text-embedding-ada-002
    (same 1536 dims, DIFFERENT model — the silent-degradation case) -> raises,
    message names BOTH the stamped and the configured model AND the re-index
    remedy;
  * same stamp + configured text-embedding-3-large (3072 dims) -> raises on the
    dim mismatch (with the column-migration step in the remedy);
  * control: configured text-embedding-3-small (matching) -> no raise (startup
    proceeds);
plus the no-op edges: empty corpus (no stamp) never blocks startup, and an exact
model+dim match passes silently.

Run:
    python -m backend.test_embedder_guard
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from embeddings import EmbeddingStamp, check_embedder_drift  # noqa: E402

# The corpus-as-indexed stamp the PRD validation test starts from.
_STAMP = EmbeddingStamp(model="text-embedding-3-small", dim=1536)


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_same_dims_different_model_fails_closed() -> None:
    """PRD core case: a corpus stamped text-embedding-3-small (1536) with the
    embedder swapped to text-embedding-ada-002 — also 1536-dim, so NOTHING
    errors at the vector layer — must refuse to start. The message must name
    BOTH the stamped and the configured model AND carry the re-index remedy."""
    try:
        # ada-002 returns 1536-dim vectors, identical to the stamp's dim.
        check_embedder_drift("text-embedding-ada-002", 1536, _STAMP)
    except RuntimeError as e:
        text = str(e)
        _check(
            "text-embedding-3-small" in text,
            f"error must name the stamped model, got: {text!r}",
        )
        _check(
            "text-embedding-ada-002" in text,
            f"error must name the configured model, got: {text!r}",
        )
        _check(
            "re-embed" in text.lower(),
            f"error must carry the re-index (re-embed) remedy, got: {text!r}",
        )
        print("ok: same-dims-different-model fails closed with both models + remedy")
        return
    raise AssertionError(
        "same-dims-different-model must raise (the dangerous silent-degradation case)"
    )


def test_different_dims_fails_closed() -> None:
    """Second PRD case: text-embedding-3-large produces 3072-dim vectors against
    a 1536-dim corpus — a structural mismatch that must refuse to start, and the
    remedy must mention the column migration the dim change requires."""
    try:
        check_embedder_drift("text-embedding-3-large", 3072, _STAMP)
    except RuntimeError as e:
        text = str(e)
        _check("3072" in text and "1536" in text, f"error must name both dims, got: {text!r}")
        _check(
            "vector(" in text.lower() or "column" in text.lower(),
            f"dim-drift remedy must mention the column migration, got: {text!r}",
        )
        print("ok: different-dims fails closed with both dims + column-migration remedy")
        return
    raise AssertionError("a dim mismatch must raise (retrieval is structurally broken)")


def test_matching_embedder_starts() -> None:
    """The control: the configured embedder matches the stamp exactly (model AND
    dim), so the guard is a silent no-op and startup proceeds."""
    check_embedder_drift("text-embedding-3-small", 1536, _STAMP)
    print("ok: matching model + dim passes silently (startup proceeds)")


def test_empty_corpus_does_not_block_startup() -> None:
    """An empty corpus has no stamp yet — there is nothing to drift from, so the
    guard never blocks startup, even for an arbitrary configured embedder/dim."""
    check_embedder_drift("text-embedding-3-large", 3072, None)
    check_embedder_drift("anything", 7, None)
    print("ok: an empty corpus (no stamp) never blocks startup")


def test_model_drift_dominates_message_even_when_dims_match() -> None:
    """When only the model drifts (dims equal), the message must be the
    same-dims wording — explicitly calling out that nothing errors — not the
    dimension-mismatch wording, so the operator understands it is the silent
    case, not a structural one."""
    try:
        check_embedder_drift("some-other-1536-model", 1536, _STAMP)
    except RuntimeError as e:
        text = str(e).lower()
        _check(
            "silently degrade" in text or "different 'languages'" in text or "silent" in text,
            f"same-dims drift must flag the silent-degradation nature, got: {text!r}",
        )
        print("ok: model-only drift surfaces the silent-degradation wording")
        return
    raise AssertionError("a model-only drift (same dims) must still raise")


def main() -> int:
    tests = [
        test_same_dims_different_model_fails_closed,
        test_different_dims_fails_closed,
        test_matching_embedder_starts,
        test_empty_corpus_does_not_block_startup,
        test_model_drift_dominates_message_even_when_dims_match,
    ]
    for t in tests:
        t()
    print(f"\nPASS: {len(tests)} embedder-guard test groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
