"""US-035: diff two retrieval-eval JSON outputs and emit a markdown comment.

The PR CI workflow runs the eval twice — once against the PR head, once
against `main` — and pipes the two JSON files through this script. The
output is a single markdown block intended to be posted as a PR comment
via `actions/github-script` and updated in place on each push.

The first line of the output is a hidden HTML marker
(`<!-- retrieval-eval-bot-comment -->`) that the github-script step keys on
when locating the prior comment to update — avoids stacking a new comment
on every push.

Usage:
    python -m evals.retrieval.ci.diff_results <main.json> <pr.json> > comment.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Recognised by the github-script step in retrieval-eval.yml; do not change
# the literal text without updating the workflow.
COMMENT_MARKER = "<!-- retrieval-eval-bot-comment -->"

# Headline-table metrics, in display order.
HEADLINE_METRICS = (
    ("recall_at_5", "recall@5"),
    ("mrr", "MRR"),
    ("ndcg_at_5", "nDCG@5"),
)

CATEGORY_ORDER = ("single_chunk", "multi_hop", "adversarial", "paraphrase", "lexical")

# Treat anything within ±0.0005 as flat — under the rounding floor used by
# the runner's `round(..., 4)` aggregator, so the noise band matches.
FLAT_THRESHOLD = 0.0005


def fmt_cell(main_v: float | None, pr_v: float) -> str:
    """Format a `pr (Δ vs main)` cell — green/red/flat arrow + signed delta."""
    if main_v is None:
        return f"{pr_v:.3f} (new)"
    delta = pr_v - main_v
    if abs(delta) < FLAT_THRESHOLD:
        return f"{pr_v:.3f} (±0.000)"
    arrow = "🟢" if delta > 0 else "🔴"
    return f"{pr_v:.3f} ({arrow} {delta:+.3f})"


def render_comment(main_data: dict[str, Any], pr_data: dict[str, Any]) -> str:
    pr_agg = pr_data.get("aggregates", {})
    main_agg = main_data.get("aggregates", {})
    pr_by_mode = pr_agg.get("by_mode", {})
    main_by_mode = main_agg.get("by_mode", {})
    pr_by_cat = pr_agg.get("by_mode_category", {})
    main_by_cat = main_agg.get("by_mode_category", {})

    modes = pr_data.get("modes", list(pr_by_mode.keys()))
    n_questions = pr_data.get("n_questions", "?")
    elapsed_pr = pr_data.get("elapsed_s", "?")
    elapsed_main = main_data.get("elapsed_s", "?")
    n_corpus = pr_data.get("n_corpus_chunks", "?")

    lines: list[str] = [
        COMMENT_MARKER,
        "## Retrieval eval — PR vs `main`",
        "",
        (
            f"n = **{n_questions}** questions × {len(modes)} modes "
            f"(`{', '.join(modes)}`) on a {n_corpus}-chunk corpus. "
            f"PR ran in {elapsed_pr}s; `main` in {elapsed_main}s."
        ),
        "",
        "### Headline (each cell: PR value, Δ vs `main`)",
        "",
        "| Mode | " + " | ".join(label for _, label in HEADLINE_METRICS) + " |",
        "|---|" + "---|" * len(HEADLINE_METRICS),
    ]

    for mode in modes:
        if mode not in pr_by_mode:
            continue
        row = [mode]
        for metric, _ in HEADLINE_METRICS:
            pr_v = float(pr_by_mode[mode][metric])
            main_v = (
                float(main_by_mode[mode][metric])
                if mode in main_by_mode and metric in main_by_mode[mode]
                else None
            )
            row.append(fmt_cell(main_v, pr_v))
        lines.append("| " + " | ".join(row) + " |")

    lines += [
        "",
        "### Per-category recall@5",
        "",
        "| Mode | " + " | ".join(CATEGORY_ORDER) + " |",
        "|---|" + "---|" * len(CATEGORY_ORDER),
    ]

    for mode in modes:
        if mode not in pr_by_cat:
            continue
        row = [mode]
        for category in CATEGORY_ORDER:
            if category not in pr_by_cat[mode]:
                row.append("—")
                continue
            pr_v = float(pr_by_cat[mode][category]["recall_at_5"])
            main_v: float | None = None
            if mode in main_by_cat and category in main_by_cat[mode]:
                main_v = float(main_by_cat[mode][category]["recall_at_5"])
            row.append(fmt_cell(main_v, pr_v))
        lines.append("| " + " | ".join(row) + " |")

    lines += [
        "",
        (
            "<sub>Comment is updated in place on each push by "
            "`.github/workflows/retrieval-eval.yml` (US-035). "
            "Comment-only — never blocks the build.</sub>"
        ),
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render a PR-comment markdown diff between two retrieval-eval JSONs.",
    )
    parser.add_argument("main_json", type=Path, help="results JSON from main branch")
    parser.add_argument("pr_json", type=Path, help="results JSON from PR head")
    args = parser.parse_args()

    main_data = json.loads(args.main_json.read_text(encoding="utf-8"))
    pr_data = json.loads(args.pr_json.read_text(encoding="utf-8"))
    sys.stdout.write(render_comment(main_data, pr_data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
