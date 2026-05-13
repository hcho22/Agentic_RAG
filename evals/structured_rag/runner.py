"""US-031: structured-RAG evaluation harness.

Runs the 30-question eval comparing the **naive** text-to-SQL path
(`backend.text_to_sql.generate_sql_naive`) against the **semantic** path
(`backend.planner.plan_query` → `backend.sql_compiler.compile_plan`). Each
question's gold value comes from executing a hand-written reference SQL in
`gold.yaml` against the seeded `crm` schema.

Run:
    python -m evals.structured_rag.runner

Reads `CRM_DATABASE_URL` (or falls back to `ANALYTICS_DATABASE_URL`) and
`OPENAI_API_KEY` from the env. Writes:
    evals/structured_rag/results.json   — per-question detail + aggregates
    evals/structured_rag/summary.md     — markdown fragment ready to drop
                                          into docs/structured-rag.md

Scoring: binary per question via normalized result-set match. Rows are
sorted lexicographically by stringified cell tuples, numerics are rounded
to 2 decimal places, column names are ignored. The headline number is the
overall % delta between naive and semantic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import yaml
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# Local imports must come after the sys.path manipulation above — flake8/ruff
# will warn, but the alternative is a packaging refactor out of scope for US-031.
from planner import PlanMatched, PlanNoMatch, plan_query  # noqa: E402
from semantic_layer import (  # noqa: E402
    SemanticLayer,
    load_and_validate,
    parse_yaml as parse_semantic_layer,
)
from sql_compiler import CompileError, compile_plan  # noqa: E402
from text_to_sql import (  # noqa: E402
    SqlSafetyError,
    generate_sql_naive,
    get_schema_snapshot,
    validate_sql_safety,
)

log = logging.getLogger("agentic_rag.evals.structured_rag")

DEFAULT_QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"
DEFAULT_GOLD = Path(__file__).resolve().parent / "gold.yaml"
DEFAULT_RESULTS = Path(__file__).resolve().parent / "results.json"
DEFAULT_SUMMARY = Path(__file__).resolve().parent / "summary.md"

CRM_SCHEMAS = ("crm",)
DEFAULT_ROW_LIMIT = 200
QUERY_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Result normalisation + comparison
# ---------------------------------------------------------------------------


def _normalise_cell(v: Any) -> Any:
    """Coerce a single Postgres value into a comparable form: numerics round
    to 2dp, dates/datetimes become ISO strings, NULLs are None. Lists/dicts
    aren't expected in the eval surface and pass through verbatim."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return round(float(v), 2)
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, (datetime, date)):
        # Compare timestamps at the second level — date_trunc produces
        # timestamptz, but two same-bucket values can differ in TZ.
        return v.isoformat()
    return v


def _normalise_rows(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Drop column names; emit a sorted list of value-tuples in deterministic
    order. Lexicographic stringification handles mixed-type columns without
    needing per-column metadata."""
    out: list[tuple[Any, ...]] = []
    for r in rows:
        out.append(tuple(_normalise_cell(v) for v in r.values()))
    out.sort(key=lambda t: tuple(str(x) for x in t))
    return out


def results_match(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> bool:
    """Compare two row-lists for equivalence after normalisation. Empty vs
    non-empty mismatches return False straightaway — this is a property of
    the eval where every gold has at least one row."""
    return _normalise_rows(a) == _normalise_rows(b)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def _exec(conn: asyncpg.Connection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Run a SELECT and return rows-as-dicts. Used for gold reference SQL,
    naive SQL, and compiled-semantic SQL alike so the comparison stays
    apples-to-apples."""
    records = await conn.fetch(sql, *(params or []), timeout=QUERY_TIMEOUT_S)
    return [{k: r[k] for k in r.keys()} for r in records]


def _safe_strip(s: str | None) -> str:
    return (s or "").strip()


# ---------------------------------------------------------------------------
# Per-question paths
# ---------------------------------------------------------------------------


async def run_gold(conn: asyncpg.Connection, reference_sql: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    try:
        return await _exec(conn, reference_sql), None
    except Exception as e:  # noqa: BLE001
        return None, f"gold-sql-failed: {e}"


async def run_naive(
    *,
    conn: asyncpg.Connection,
    openai_client: AsyncOpenAI,
    question: str,
    schema_snapshot: str,
) -> dict[str, Any]:
    """Run the naive text-to-SQL baseline: LLM generates SQL from a raw
    column dump (no metric definitions), we validate + execute. Returns a
    dict with `sql`, `rows`, and optionally `error`."""
    try:
        sql = await generate_sql_naive(
            openai_client=openai_client,
            question=question,
            schemas=CRM_SCHEMAS,
            row_limit=DEFAULT_ROW_LIMIT,
            schema_snapshot=schema_snapshot,
        )
    except Exception as e:  # noqa: BLE001
        return {"sql": None, "rows": None, "error": f"generation-failed: {e}"}

    try:
        validate_sql_safety(sql, CRM_SCHEMAS)
    except SqlSafetyError as e:
        return {"sql": sql, "rows": None, "error": f"unsafe-sql: {e}"}

    try:
        rows = await _exec(conn, sql)
        return {"sql": sql, "rows": rows, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"sql": sql, "rows": None, "error": f"exec-failed: {e}"}


async def run_semantic(
    *,
    conn: asyncpg.Connection,
    openai_client: AsyncOpenAI,
    question: str,
    layer: SemanticLayer,
) -> dict[str, Any]:
    """Run the semantic path: planner → compiler → execute. Returns the
    plan, the compiled SQL, the rows, and optionally an error / no_match.
    """
    try:
        plan_result = await plan_query(
            openai_client=openai_client,
            question=question,
            layer=layer,
        )
    except Exception as e:  # noqa: BLE001
        return {"plan": None, "sql": None, "rows": None, "error": f"plan-failed: {e}"}

    if isinstance(plan_result, PlanNoMatch):
        return {
            "plan": None,
            "sql": None,
            "rows": None,
            "error": f"no_match: {plan_result.reason}",
            "no_match": True,
            "suggested_fallback": plan_result.suggested_fallback,
        }

    assert isinstance(plan_result, PlanMatched)
    plan = plan_result.plan
    try:
        sql, params = compile_plan(plan, layer, row_limit=DEFAULT_ROW_LIMIT)
    except CompileError as e:
        return {"plan": plan.model_dump(), "sql": None, "rows": None, "error": f"compile-failed: {e}"}

    try:
        validate_sql_safety(sql, CRM_SCHEMAS)
    except SqlSafetyError as e:
        return {"plan": plan.model_dump(), "sql": sql, "rows": None, "error": f"unsafe-sql: {e}"}

    try:
        rows = await _exec(conn, sql, params)
        return {"plan": plan.model_dump(), "sql": sql, "rows": rows, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"plan": plan.model_dump(), "sql": sql, "rows": None, "error": f"exec-failed: {e}"}


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def _load_yaml_list(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a top-level list")
    return data


def _get_db_url() -> str:
    for var in ("CRM_DATABASE_URL", "ANALYTICS_DATABASE_URL"):
        raw = os.environ.get(var)
        if raw and raw.strip():
            return raw.strip()
    raise RuntimeError(
        "set CRM_DATABASE_URL (or ANALYTICS_DATABASE_URL) — the eval needs "
        "to read from the seeded crm schema"
    )


async def main(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    questions = _load_yaml_list(Path(args.questions))
    gold_entries = _load_yaml_list(Path(args.gold))
    gold_by_id = {g["id"]: g for g in gold_entries}

    layer = parse_semantic_layer()
    db_url = _get_db_url()

    # Live-validate the layer against the DB up front — running the eval
    # against a layer that doesn't match the schema would produce
    # meaningless scores, so we fail loudly here.
    await load_and_validate(database_url=db_url)

    openai_client = AsyncOpenAI()
    conn = await asyncpg.connect(db_url, timeout=10.0)
    try:
        schema_snapshot = await get_schema_snapshot(db_url, CRM_SCHEMAS)
    finally:
        # get_schema_snapshot opens its own connection internally — the conn
        # above stays open for the eval queries.
        pass

    per_question: list[dict[str, Any]] = []
    t_start = time.time()
    try:
        for q in questions:
            qid = q["id"]
            qtext = q["question"]
            category = q["category"]
            gold = gold_by_id.get(qid)
            if not gold:
                log.warning("no gold for %s; skipping", qid)
                continue

            log.info("running %s [%s]: %s", qid, category, qtext)
            gold_rows, gold_err = await run_gold(conn, gold["reference_sql"])
            naive = await run_naive(
                conn=conn,
                openai_client=openai_client,
                question=qtext,
                schema_snapshot=schema_snapshot,
            )
            semantic = await run_semantic(
                conn=conn,
                openai_client=openai_client,
                question=qtext,
                layer=layer,
            )

            naive_correct = (
                gold_rows is not None
                and naive["error"] is None
                and naive["rows"] is not None
                and results_match(gold_rows, naive["rows"])
            )
            semantic_correct = (
                gold_rows is not None
                and semantic["error"] is None
                and semantic["rows"] is not None
                and results_match(gold_rows, semantic["rows"])
            )

            per_question.append(
                {
                    "id": qid,
                    "category": category,
                    "question": qtext,
                    "gold": {
                        "sql": gold["reference_sql"].strip(),
                        "rows": gold_rows,
                        "error": gold_err,
                    },
                    "naive": {**naive, "correct": naive_correct},
                    "semantic": {**semantic, "correct": semantic_correct},
                }
            )
    finally:
        await conn.close()

    elapsed_s = time.time() - t_start

    # Aggregates
    by_cat_naive: dict[str, list[bool]] = defaultdict(list)
    by_cat_semantic: dict[str, list[bool]] = defaultdict(list)
    for r in per_question:
        by_cat_naive[r["category"]].append(r["naive"]["correct"])
        by_cat_semantic[r["category"]].append(r["semantic"]["correct"])

    def pct(bools: list[bool]) -> float:
        return round(100.0 * sum(bools) / len(bools), 1) if bools else 0.0

    naive_overall = pct([r["naive"]["correct"] for r in per_question])
    semantic_overall = pct([r["semantic"]["correct"] for r in per_question])
    aggregates = {
        "overall": {
            "naive": naive_overall,
            "semantic": semantic_overall,
            "delta_pp": round(semantic_overall - naive_overall, 1),
            "n": len(per_question),
        },
        "by_category": {
            cat: {
                "naive": pct(by_cat_naive[cat]),
                "semantic": pct(by_cat_semantic[cat]),
                "delta_pp": round(pct(by_cat_semantic[cat]) - pct(by_cat_naive[cat]), 1),
                "n": len(by_cat_naive[cat]),
            }
            for cat in sorted(by_cat_naive)
        },
        "elapsed_s": round(elapsed_s, 1),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    json_path = Path(args.output_json)
    json_path.write_text(
        json.dumps(
            {"aggregates": aggregates, "questions": per_question},
            indent=2,
            default=_json_default,
        ),
        encoding="utf-8",
    )
    md_path = Path(args.output_md)
    md_path.write_text(
        _render_markdown(aggregates, per_question),
        encoding="utf-8",
    )

    print(
        f"\nNaive: {naive_overall}%   Semantic: {semantic_overall}%   "
        f"Δ {aggregates['overall']['delta_pp']:+}pp  (n={len(per_question)})"
    )
    print(f"results → {json_path}")
    print(f"summary → {md_path}")
    return 0


def _json_default(o: Any) -> Any:
    """Make Decimals + datetimes JSON-serialisable so results.json is
    inspectable without a custom decoder."""
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


# ---------------------------------------------------------------------------
# Markdown summary — drop into docs/structured-rag.md's Evaluation section.
# ---------------------------------------------------------------------------


def _render_markdown(aggregates: dict[str, Any], per_question: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append(f"_Generated by `python -m evals.structured_rag.runner` at {aggregates['generated_at']} (eval ran in {aggregates['elapsed_s']}s)._")
    lines.append("")
    o = aggregates["overall"]
    lines.append(f"**Headline:** naive **{o['naive']}%** vs semantic **{o['semantic']}%** — Δ **{o['delta_pp']:+}pp** on n={o['n']} questions.")
    lines.append("")
    lines.append("### Per-category accuracy")
    lines.append("")
    lines.append("| Category | n | Naive | Semantic | Δ |")
    lines.append("|---|---|---|---|---|")
    for cat, agg in aggregates["by_category"].items():
        lines.append(
            f"| {cat} | {agg['n']} | {agg['naive']}% | {agg['semantic']}% | {agg['delta_pp']:+}pp |"
        )
    lines.append("")
    lines.append("### Per-question outcome")
    lines.append("")
    lines.append("| ID | Category | Naive | Semantic | Question |")
    lines.append("|---|---|---|---|---|")
    for r in per_question:
        n_mark = "✅" if r["naive"]["correct"] else "❌"
        s_mark = "✅" if r["semantic"]["correct"] else "❌"
        q_safe = r["question"].replace("|", "\\|")
        lines.append(f"| {r['id']} | {r['category']} | {n_mark} | {s_mark} | {q_safe} |")
    lines.append("")

    examples = [r for r in per_question if (not r["naive"]["correct"]) and r["semantic"]["correct"]][:3]
    if examples:
        lines.append("### Naive→Semantic before/after")
        lines.append("")
        for r in examples:
            lines.append(f"**{r['id']} — {r['question']}**")
            lines.append("")
            lines.append("Naive SQL:")
            lines.append("```sql")
            lines.append(_safe_strip(r["naive"].get("sql")) or "(no SQL produced)")
            lines.append("```")
            sem_plan = r["semantic"].get("plan")
            if sem_plan:
                lines.append(f"Semantic plan: `{json.dumps(sem_plan, default=str)}`")
                lines.append("")
            lines.append("Semantic SQL:")
            lines.append("```sql")
            lines.append(_safe_strip(r["semantic"].get("sql")) or "(no SQL produced)")
            lines.append("```")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the structured-RAG eval.")
    p.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    p.add_argument("--gold", default=str(DEFAULT_GOLD))
    p.add_argument("--output-json", default=str(DEFAULT_RESULTS))
    p.add_argument("--output-md", default=str(DEFAULT_SUMMARY))
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_parse_args())))
