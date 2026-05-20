"""US-044: refresh `docs/permissions-aware-rag.md` from runner-generated summaries.

The doc carries named markers — `<!-- BEGIN EVAL_SUMMARY:retrieval -->` and
`<!-- BEGIN EVAL_SUMMARY:permissions_scale -->` — bracketing regions that
are *generated*, not hand-written. This script reads each runner's
`summary.md`, strips its own outer markers (`<!-- BEGIN/END EVAL_SUMMARY -->`),
and replaces the bracketed region in the doc.

Run after either runner produces new numbers:

    python -m evals.retrieval.runner
    python -m evals.permissions_scale.runner
    python -m docs._embed_eval_summaries

The script is idempotent: running it twice with no source changes leaves
the doc byte-identical.

Adding a new embed target later means: (1) drop a `<!-- BEGIN EVAL_SUMMARY:NAME -->`
+ `<!-- END EVAL_SUMMARY:NAME -->` pair into the target doc, (2) add a
new entry to `EMBEDS` below.

US-004 adds a second destination doc: the RAGAS comparison table is lifted
out of the retrieval `summary.md` (the region between its `EVAL_SUMMARY_RAGAS`
markers) and embedded into the matching marker pair in `docs/evals.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "permissions-aware-rag.md"

# Each entry: marker name → source `summary.md` path. The marker name
# appears in both the BEGIN and END comments in TARGET.
EMBEDS: dict[str, Path] = {
    "retrieval": ROOT / "evals" / "retrieval" / "summary.md",
    "permissions_scale": ROOT / "evals" / "permissions_scale" / "summary.md",
}

OUTER_MARKER_RE = re.compile(
    r"<!-- (?:BEGIN|END) EVAL_SUMMARY -->\s*\n?", re.MULTILINE
)

# US-004: the RAGAS comparison table is embedded into docs/evals.md, separate
# from the named-region embeds above. The retrieval runner brackets the table
# in summary.md with these markers; the same pair brackets the destination
# region in evals.md.
EVALS_DOC = ROOT / "docs" / "evals.md"
RAGAS_START = "<!-- EVAL_SUMMARY_RAGAS_START -->"
RAGAS_END = "<!-- EVAL_SUMMARY_RAGAS_END -->"


def extract_payload(summary_md: str) -> str:
    """Strip the source's own outer `EVAL_SUMMARY` markers + surrounding blank lines.

    The runner-generated `summary.md` opens and closes with `<!-- BEGIN
    EVAL_SUMMARY -->` / `<!-- END EVAL_SUMMARY -->`. Embedding those
    inside the doc's own named-marker region would create nested markers
    and confuse a future round-tripper. Strip them and trim the leading/
    trailing whitespace; the doc supplies its own framing.
    """
    payload = OUTER_MARKER_RE.sub("", summary_md)
    return payload.strip()


def replace_region(doc: str, name: str, payload: str) -> str:
    """Replace content between `BEGIN EVAL_SUMMARY:<name>` / `END EVAL_SUMMARY:<name>`."""
    pattern = re.compile(
        rf"(<!-- BEGIN EVAL_SUMMARY:{re.escape(name)} -->)"
        r".*?"
        rf"(<!-- END EVAL_SUMMARY:{re.escape(name)} -->)",
        re.DOTALL,
    )
    if not pattern.search(doc):
        raise RuntimeError(
            f"marker pair not found in {TARGET}: "
            f"<!-- BEGIN EVAL_SUMMARY:{name} --> ... <!-- END EVAL_SUMMARY:{name} -->"
        )
    # Build the replacement from match groups inside the lambda — passing
    # `\1\n\n...\2` as a string to a function-arg `sub` would treat the
    # backrefs as literal characters, not capture groups (and erase the
    # markers). The lambda also makes the payload's contents safe even
    # if it happens to include `\1`-style sequences.
    return pattern.sub(
        lambda m: f"{m.group(1)}\n\n{payload}\n\n{m.group(2)}",
        doc,
        count=1,
    )


def extract_ragas_table(summary_md: str) -> str | None:
    """Return the RAGAS table bracketed by the EVAL_SUMMARY_RAGAS markers.

    Returns None when the markers are absent — a `summary.md` generated before
    US-004, or never generated at all — so the caller falls back to a
    placeholder rather than crashing.
    """
    pattern = re.compile(
        rf"{re.escape(RAGAS_START)}(.*?){re.escape(RAGAS_END)}", re.DOTALL
    )
    match = pattern.search(summary_md)
    return match.group(1).strip() if match else None


def replace_ragas_region(doc: str, payload: str) -> str:
    """Replace the content between the EVAL_SUMMARY_RAGAS markers in `doc`."""
    pattern = re.compile(
        rf"({re.escape(RAGAS_START)})(.*?)({re.escape(RAGAS_END)})", re.DOTALL
    )
    if not pattern.search(doc):
        raise RuntimeError(
            f"marker pair not found in {EVALS_DOC}: "
            f"{RAGAS_START} ... {RAGAS_END}"
        )
    return pattern.sub(
        lambda m: f"{m.group(1)}\n\n{payload}\n\n{m.group(3)}",
        doc,
        count=1,
    )


def main() -> None:
    if not TARGET.exists():
        raise SystemExit(f"target doc missing: {TARGET}")
    doc = TARGET.read_text(encoding="utf-8")
    refreshed: list[str] = []
    for name, source_path in EMBEDS.items():
        if not source_path.exists():
            placeholder = (
                f"_`{source_path.relative_to(ROOT)}` not found yet — "
                f"run the corresponding runner to populate this section._"
            )
            doc = replace_region(doc, name, placeholder)
            refreshed.append(f"  {name}: PLACEHOLDER (source missing)")
            continue
        payload = extract_payload(source_path.read_text(encoding="utf-8"))
        doc = replace_region(doc, name, payload)
        refreshed.append(f"  {name}: from {source_path.relative_to(ROOT)}")
    TARGET.write_text(doc, encoding="utf-8")

    # US-004: lift the RAGAS comparison table out of the retrieval summary.md
    # and embed it into docs/evals.md's EVAL_SUMMARY_RAGAS region.
    if not EVALS_DOC.exists():
        raise SystemExit(f"target doc missing: {EVALS_DOC}")
    retrieval_summary = EMBEDS["retrieval"]
    ragas_table: str | None = None
    if retrieval_summary.exists():
        ragas_table = extract_ragas_table(
            retrieval_summary.read_text(encoding="utf-8")
        )
    if ragas_table is None:
        ragas_table = (
            "_RAGAS comparison not generated yet — run "
            "`python -m evals.retrieval.runner --include-ragas`._"
        )
        refreshed.append("  ragas: PLACEHOLDER (no RAGAS table in summary.md)")
    else:
        refreshed.append(
            f"  ragas: from {retrieval_summary.relative_to(ROOT)} "
            f"→ {EVALS_DOC.relative_to(ROOT)}"
        )
    evals_doc = replace_ragas_region(
        EVALS_DOC.read_text(encoding="utf-8"), ragas_table
    )
    EVALS_DOC.write_text(evals_doc, encoding="utf-8")

    print("refreshed eval-summary embeds:")
    print("\n".join(refreshed))


if __name__ == "__main__":
    main()
