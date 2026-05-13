"""US-030: deterministic PlanSpec → SQL compiler.

The planner picks WHAT to measure (which metric, which dimensions, which
filters); this compiler turns that into a runnable Postgres SELECT using
the metric `sql_fragment`s from `semantic_layer.yaml` plus the join graph.
There is no LLM call here — given the same PlanSpec and SemanticLayer,
this function emits byte-identical SQL every run. That property is what
makes the US-031 eval reproducible.

Two compilation strategies:

  * **inline** metrics carry an aggregate SQL expression (e.g.
    `SUM(crm.orders.total) FILTER (WHERE ...)`). The compiler joins the
    needed entities, places the expression in the outer SELECT alongside
    dimension columns, and groups by the dimensions.

  * **scalar** metrics carry a self-contained `(SELECT ...)` expression.
    The compiler emits each as a standalone SELECT; combining a scalar
    metric with dimensions is rejected at compile time because the scalar
    already collapses to one row.

`sql_search` is the agent-facing tool wrapper. It accepts the planner's
PlanSpec, compiles it, runs the result through the existing
`validate_sql_safety` (defense in depth), and executes via the same
read-only transaction + statement-timeout path `query_database` used.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from planner import Filter, FilterOp, PlanSpec, TimeGrain
from semantic_layer import Dimension, Entity, Join, Metric, SemanticLayer
from text_to_sql import (
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
    QueryDatabaseResult,
    SqlSafetyError,
    _execute_select,
    get_allowed_schemas,
    get_query_timeout_ms,
    validate_sql_safety,
)

log = logging.getLogger("agentic_rag.backend.sql_compiler")


class CompileError(ValueError):
    """Raised when a PlanSpec cannot be compiled — e.g. mixing scalar and
    inline metrics, or asking for dimensions on a scalar metric. The
    message is shown to the agent so it can react (re-plan, fall back,
    or surface the limitation to the user)."""


def _op_sql(op: FilterOp) -> str:
    return {
        "eq": "=", "neq": "<>",
        "gt": ">", "gte": ">=",
        "lt": "<", "lte": "<=",
        "in": "IN", "between": "BETWEEN",
    }[op]


def _quote_literal(v: Any) -> str:
    """Quote a value for inline embedding in SQL. We avoid this for any
    user-controllable input — filter values flow through the parameterized
    `$N` path — but date-grain literals like `'month'` go through here."""
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if v is None:
        return "NULL"
    return str(v)


def _dim_column_ref(dim: Dimension, layer: SemanticLayer) -> str:
    """Fully-qualified `schema.table.column` reference for a dimension.
    The compiler uses this in SELECT, WHERE, and GROUP BY so SQL stays
    unambiguous when multiple entities are joined."""
    entity = layer.entities[dim.entity]
    return f"{entity.table}.{dim.column}"


def _dim_select_expr(
    dim: Dimension,
    time_grain: TimeGrain | None,
    layer: SemanticLayer,
) -> str:
    """SELECT-side expression for a dimension. Time-kind dims get wrapped
    in `date_trunc(grain, col)` when a time_grain is requested; everything
    else is the bare column reference."""
    col_ref = _dim_column_ref(dim, layer)
    if dim.kind == "time" and time_grain is not None:
        return f"date_trunc({_quote_literal(time_grain)}, {col_ref})"
    return col_ref


def _resolve_root_and_joins(
    needed: set[str], layer: SemanticLayer
) -> tuple[str, list[Join]]:
    """Pick a FROM root and BFS over the join graph to reach every needed
    entity. Returns (root_entity_name, ordered_joins). Raises CompileError
    if the join graph leaves some needed entity unreachable."""
    if not needed:
        raise CompileError("plan has no entities to compile against")

    # Choose the entity with the most direct connections among the needed
    # set — minimises chained joins. Ties broken by name for determinism.
    adjacency: dict[str, list[Join]] = {n: [] for n in layer.entities}
    for j in layer.joins:
        adjacency[j.from_entity].append(j)
        # Add the reverse edge as a swapped Join so traversal can come from
        # either side. The predicate is symmetric (a = b) so the SQL works
        # either way.
        adjacency[j.to_entity].append(
            Join(**{"from": j.to_entity, "to": j.from_entity, "predicate": j.predicate})
        )

    def score(name: str) -> tuple[int, str]:
        # Number of edges to entities also in `needed`. Higher is better;
        # break ties by name ascending so the choice is stable.
        return (
            -sum(1 for j in adjacency[name] if j.to_entity in needed),
            name,
        )

    root = min(needed, key=score)
    visited = {root}
    ordered: list[Join] = []
    queue: deque[str] = deque([root])
    while queue:
        node = queue.popleft()
        for j in adjacency[node]:
            if j.to_entity in needed and j.to_entity not in visited:
                visited.add(j.to_entity)
                ordered.append(j)
                queue.append(j.to_entity)

    unreached = needed - visited
    if unreached:
        raise CompileError(
            f"join graph leaves entities unreached from {root!r}: "
            f"{sorted(unreached)}"
        )
    return root, ordered


def _coerce_time_value(v: Any) -> Any:
    """asyncpg validates parameter types at bind time — passing a str to a
    timestamptz column raises before the SQL-level cast ever runs. For
    time-kind dim values we parse ISO 8601 strings to a UTC-aware
    `datetime`. Plain `date` won't do: Postgres casts `date::timestamptz`
    using the *system* TZ (not the session TZ), so a naive date can shift
    by hours from what the user meant. UTC is the canonical assumption for
    unqualified BI date filters; the planner's prompt nudges the model
    toward ISO date strings in UTC.
    """
    if isinstance(v, datetime):
        return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)
    if isinstance(v, date):  # plain date, not datetime
        return datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if not isinstance(v, str):
        return v
    s = v.strip()
    try:
        if len(s) == 10:
            d = date.fromisoformat(s)
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        # Leave malformed strings alone — asyncpg's error message is more
        # actionable than a re-raise we'd have to compose here.
        return v


def _filter_clause(
    f: Filter, layer: SemanticLayer, param_start: int
) -> tuple[str, list[Any]]:
    """Render a single Filter as `<col_ref> <op> <placeholder>` with the
    matching parameter values. Returns (clause, params). `param_start` is
    the next available `$N` index so asyncpg's positional binding stays
    consistent across multiple filters."""
    dim = layer.dimensions[f.dimension]
    col = _dim_column_ref(dim, layer)
    op = _op_sql(f.op)
    # asyncpg's positional binding requires the runtime type to match the
    # column type — a `str` won't bind to a timestamptz column even with a
    # SQL-level `::timestamptz` cast (it errors before reaching Postgres).
    # For time-kind dims we both coerce the Python value to `date`/`datetime`
    # AND emit the cast as defense in depth.
    is_time = dim.kind == "time"
    cast = "::timestamptz" if is_time else ""

    def prep(v: Any) -> Any:
        return _coerce_time_value(v) if is_time else v

    if f.op == "between":
        # Validated upstream — value is a 2-element list. For time-kind
        # dimensions we emit a half-open range (`>= low AND < high`) to
        # match the BI convention "Jan-Mar" = [2026-01-01, 2026-04-01).
        # `BETWEEN` is inclusive at both ends, which over-counts boundary
        # rows when the model uses start-of-next-period as the upper bound
        # (the standard pattern we instruct in the planner prompt).
        if is_time:
            return (
                f"{col} >= ${param_start}{cast} AND {col} < ${param_start + 1}{cast}",
                [prep(x) for x in f.value],  # type: ignore[arg-type]
            )
        return (
            f"{col} {op} ${param_start}{cast} AND ${param_start + 1}{cast}",
            list(f.value),  # type: ignore[arg-type]
        )
    if f.op == "in":
        # asyncpg supports IN via = ANY($1::text[]). We use ANY rather than
        # building `IN ($1, $2, ...)` so the param count stays at 1
        # regardless of list size — also dodges Postgres's parameter cap.
        values = [prep(x) for x in f.value] if is_time else list(f.value)  # type: ignore[arg-type]
        return (
            f"{col} = ANY(${param_start})",
            [values],
        )
    return (f"{col} {op} ${param_start}{cast}", [prep(f.value)])


def _entities_for_plan(
    plan: PlanSpec, layer: SemanticLayer
) -> set[str]:
    """Union of every entity referenced by the plan's metrics, dimensions,
    and filter dimensions. The compiler uses this to plan FROM/JOINs."""
    needed: set[str] = set()
    for m in plan.metrics:
        needed.update(layer.metrics[m].entities)
    for d in plan.dimensions:
        needed.add(layer.dimensions[d].entity)
    for f in plan.filters:
        needed.add(layer.dimensions[f.dimension].entity)
    return needed


def _compile_scalar(
    plan: PlanSpec, layer: SemanticLayer
) -> tuple[str, list[Any]]:
    """Scalar metrics already collapse to a single row — emit them as a
    bare SELECT with no FROM. Filters and dimensions are rejected upstream
    so we don't need to integrate them here."""
    if plan.dimensions:
        raise CompileError(
            "scalar metrics cannot be combined with dimensions — "
            "this should have been caught by the planner"
        )
    if plan.filters:
        raise CompileError(
            "scalar metrics do not currently support filters — the metric's "
            "sql_fragment is self-contained"
        )
    parts = [
        f"{layer.metrics[m].sql_fragment} AS {m}" for m in plan.metrics
    ]
    return f"SELECT {', '.join(parts)}", []


def _compile_inline(
    plan: PlanSpec, layer: SemanticLayer, row_limit: int
) -> tuple[str, list[Any]]:
    needed = _entities_for_plan(plan, layer)
    root, joins = _resolve_root_and_joins(needed, layer)
    root_table = layer.entities[root].table

    # SELECT: dimensions first, metrics second. Aliases match the public
    # name so result columns line up with the planner's output.
    select_parts: list[str] = []
    group_by_parts: list[str] = []
    for d in plan.dimensions:
        dim = layer.dimensions[d]
        expr = _dim_select_expr(dim, plan.time_grain, layer)
        select_parts.append(f"{expr} AS {d}")
        group_by_parts.append(expr)
    for m in plan.metrics:
        select_parts.append(f"{layer.metrics[m].sql_fragment} AS {m}")

    sql = f"SELECT {', '.join(select_parts)}\nFROM {root_table}"
    for j in joins:
        to_table = layer.entities[j.to_entity].table
        sql += f"\nLEFT JOIN {to_table} ON {j.predicate}"

    params: list[Any] = []
    if plan.filters:
        clauses: list[str] = []
        for f in plan.filters:
            clause, vals = _filter_clause(f, layer, len(params) + 1)
            clauses.append(clause)
            params.extend(vals)
        sql += "\nWHERE " + " AND ".join(clauses)

    if group_by_parts:
        sql += "\nGROUP BY " + ", ".join(group_by_parts)
        sql += "\nORDER BY " + ", ".join(group_by_parts)
    sql += f"\nLIMIT {int(row_limit)}"
    return sql, params


def compile_plan(
    plan: PlanSpec,
    layer: SemanticLayer,
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> tuple[str, list[Any]]:
    """Compile a PlanSpec into (sql, params). Pure: given the same inputs
    on the same SemanticLayer revision, the output is byte-identical."""
    if not plan.metrics:
        raise CompileError("plan has no metrics")
    kinds = {layer.metrics[m].kind for m in plan.metrics}
    if "scalar" in kinds and "inline" in kinds:
        raise CompileError(
            "plan mixes scalar and inline metrics — split into two queries"
        )
    if "scalar" in kinds:
        return _compile_scalar(plan, layer)
    return _compile_inline(plan, layer, row_limit)


# ---------------------------------------------------------------------------
# `sql_search` agent tool — accepts a PlanSpec, compiles + executes.
# ---------------------------------------------------------------------------


class SqlSearchInput(BaseModel):
    """Tool-schema input the agent supplies. `plan` is REQUIRED, so the
    agent cannot invoke sql_search without first running plan_query — the
    architectural guarantee the PRD's US-030 acceptance criteria call out.
    """

    plan: PlanSpec = Field(
        ...,
        description=(
            "Structured plan from plan_query's matched output. Pass the "
            "PlanSpec verbatim — the compiler resolves metric SQL "
            "fragments, joins, filters, and time grain deterministically."
        ),
    )
    row_limit: int = Field(
        default=DEFAULT_ROW_LIMIT,
        ge=1,
        le=MAX_ROW_LIMIT,
        description="Soft cap on rows returned. Aggregates collapse to 1 row anyway.",
    )


def get_crm_database_url() -> str | None:
    """`CRM_DATABASE_URL` for the crm_readonly role. Falls back to
    `ANALYTICS_DATABASE_URL` so Module 7 deploys keep working until they
    point at the new role explicitly."""
    for var in ("CRM_DATABASE_URL", "ANALYTICS_DATABASE_URL"):
        raw = os.environ.get(var)
        if raw and raw.strip():
            return raw.strip()
    return None


def is_enabled() -> bool:
    """True when a DB URL is configured. Used by main.py to decide whether
    to expose plan_query + sql_search to the agent — both tools share a
    fate because sql_search is useless without plan_query."""
    return get_crm_database_url() is not None


async def sql_search(
    *,
    plan: PlanSpec,
    layer: SemanticLayer,
    row_limit: int = DEFAULT_ROW_LIMIT,
    database_url: str | None = None,
    timeout_ms: int | None = None,
    allowed_schemas: tuple[str, ...] | None = None,
) -> QueryDatabaseResult:
    """Compile a PlanSpec → SQL → execute. Returns the same shape as
    `query_database` so the frontend's SQL card renders both identically."""
    db_url = database_url or get_crm_database_url()
    if db_url is None:
        raise RuntimeError(
            "sql_search called but CRM_DATABASE_URL is not set"
        )
    schemas = allowed_schemas or get_allowed_schemas()
    timeout = timeout_ms if timeout_ms is not None else get_query_timeout_ms()

    sql, params = compile_plan(plan, layer, row_limit=row_limit)
    try:
        validate_sql_safety(sql, schemas)
    except SqlSafetyError:
        # The compiler is deterministic, so a safety failure means we
        # produced bad SQL — log loudly so it surfaces in CI / dev rather
        # than silently degrading.
        log.exception("sql_compiler.safety_violation sql=%r", sql)
        raise
    log.info(
        "sql_search.execute schemas=%s timeout_ms=%d params=%d sql=%r",
        ",".join(schemas), timeout, len(params), sql,
    )

    columns, rows, truncated = await _execute_select(
        database_url=db_url,
        sql=sql,
        timeout_ms=timeout,
        row_limit=row_limit,
        params=params,
    )
    return QueryDatabaseResult(
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )


def sql_search_tool_schema() -> dict[str, Any]:
    """Chat Completions `tools[]` entry. The `plan` field is required —
    OpenAI's function-calling validator rejects calls without it, so the
    agent cannot reach sql_search without first calling plan_query and
    receiving a matched PlanSpec."""
    return {
        "type": "function",
        "function": {
            "name": "sql_search",
            "description": (
                "Compile a planner-emitted PlanSpec into SQL against the "
                "CRM schema and execute it under a read-only Postgres "
                "role. You CANNOT call this tool without first calling "
                "plan_query and receiving a {status: \"matched\", plan: ...} "
                "result — pass that plan verbatim here. Returns rows + the "
                "compiled SQL so you can cite the SQL in your reply."
            ),
            "parameters": SqlSearchInput.model_json_schema(),
        },
    }
