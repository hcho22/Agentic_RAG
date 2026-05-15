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
    print(f"refreshed {TARGET.relative_to(ROOT)}:")
    print("\n".join(refreshed))


if __name__ == "__main__":
    main()
