"""US-029: load + validate the structured-RAG semantic layer.

`semantic_layer.yaml` is the single source of truth for entities, dimensions,
metrics (with hand-written SQL fragments), and joins. This module:

  * parses the YAML into typed Pydantic models so downstream code (planner,
    compiler) sees a stable shape;
  * cross-checks every column reference against the live Postgres schema —
    catching typos at startup, not at query time;
  * verifies that each multi-entity metric has a join path connecting its
    entities, so the compiler in US-030 can always assemble a FROM/JOIN
    clause.

A single `SemanticLayerError` is raised on any failure; main.py calls
`load_and_validate(...)` during FastAPI startup so a broken layer prevents
the app from coming up instead of producing wrong SQL.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable

import asyncpg
import yaml
from pydantic import BaseModel, Field

log = logging.getLogger("agentic_rag.backend.semantic_layer")

DEFAULT_YAML_PATH = Path(__file__).resolve().parent / "semantic_layer.yaml"


class SemanticLayerError(ValueError):
    """Raised when the semantic layer is malformed or references missing
    columns / unreachable joins. Always actionable — the message names the
    offending entity, dimension, metric, or join."""


class Entity(BaseModel):
    name: str
    table: str  # fully qualified, e.g. "crm.customers"
    primary_key: str
    description: str | None = None


class Dimension(BaseModel):
    name: str
    entity: str
    column: str
    # `time` marks dimensions backed by a timestamp/date column so the
    # compiler in US-030 can wrap them in `date_trunc(time_grain, col)` when
    # a time grain is requested. Categorical is the default and just emits
    # the bare column reference.
    kind: str = "categorical"
    description: str | None = None
    synonyms: list[str] = Field(default_factory=list)


class Metric(BaseModel):
    name: str
    description: str
    sql_fragment: str
    grain: str
    entities: list[str]
    # `inline` metrics are aggregate expressions that compose with dimensions
    # via GROUP BY; the compiler splices them into the outer SELECT alongside
    # joins. `scalar` metrics are self-contained `(SELECT ...)` subqueries
    # that already produce a 1-row answer — they cannot be combined with
    # dimensions and the compiler emits them as standalone SELECTs.
    kind: str = "inline"
    synonyms: list[str] = Field(default_factory=list)


class Join(BaseModel):
    # YAML 1.1 (PyYAML safe_load) coerces a bare `on:` key to the boolean
    # True, so the YAML uses `predicate:` instead — same semantics, none of
    # the surprise.
    from_entity: str = Field(alias="from")
    to_entity: str = Field(alias="to")
    predicate: str

    model_config = {"populate_by_name": True}


class SemanticLayer(BaseModel):
    version: int
    entities: dict[str, Entity]
    dimensions: dict[str, Dimension]
    metrics: dict[str, Metric]
    joins: list[Join]


# Catches `crm.customers.email`-style references. The first group is the
# schema, the second the table, the third the column. We use this to pull
# column references out of the metric `sql_fragment` and join `predicate`.
_QUALIFIED_COL_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b",
    re.IGNORECASE,
)

# Catches bare `crm.customers` (schema.table without a trailing `.column`),
# so metrics that use subquery aliases like `FROM crm.orders o ... o.total`
# still get table-level validation. The trailing `\b` is load-bearing: it
# stops the regex engine from backtracking to a shorter table name (e.g.
# matching `crm.order` inside `crm.orders.status`) just to satisfy the
# negative lookahead.
_QUALIFIED_TABLE_RE = re.compile(
    r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b(?!\.[a-z_])",
    re.IGNORECASE,
)


def parse_yaml(path: Path | str | None = None) -> SemanticLayer:
    """Parse the YAML into a SemanticLayer. Pure structural validation only —
    does not touch the database. Raises SemanticLayerError on shape problems.
    """
    p = Path(path) if path else DEFAULT_YAML_PATH
    if not p.exists():
        raise SemanticLayerError(f"semantic layer YAML not found at {p}")

    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise SemanticLayerError("semantic_layer.yaml must be a mapping at the top level")

    # The YAML uses entity/dimension/metric *keys* as names; the loader
    # injects them into each row so consumers can rely on `.name` without
    # juggling both the dict key and the value.
    def _inject_names(section: dict | None) -> dict:
        if section is None:
            return {}
        if not isinstance(section, dict):
            raise SemanticLayerError("entities/dimensions/metrics must be mappings")
        out: dict = {}
        for key, val in section.items():
            if not isinstance(val, dict):
                raise SemanticLayerError(f"section entry {key!r} must be a mapping")
            out[key] = {"name": key, **val}
        return out

    try:
        layer = SemanticLayer(
            version=int(raw.get("version", 0)),
            entities=_inject_names(raw.get("entities")),
            dimensions=_inject_names(raw.get("dimensions")),
            metrics=_inject_names(raw.get("metrics")),
            joins=raw.get("joins") or [],
        )
    except Exception as e:  # noqa: BLE001 — surface a single typed error
        raise SemanticLayerError(f"semantic layer failed to parse: {e}") from e

    # Cross-reference: dimensions and metrics must point at known entities.
    # Catching this here means the live-DB validator can assume entity names
    # are coherent.
    for dim in layer.dimensions.values():
        if dim.entity not in layer.entities:
            raise SemanticLayerError(
                f"dimension {dim.name!r} references unknown entity {dim.entity!r}"
            )
    for metric in layer.metrics.values():
        for ent in metric.entities:
            if ent not in layer.entities:
                raise SemanticLayerError(
                    f"metric {metric.name!r} references unknown entity {ent!r}"
                )
    for join in layer.joins:
        if join.from_entity not in layer.entities:
            raise SemanticLayerError(
                f"join references unknown entity {join.from_entity!r}"
            )
        if join.to_entity not in layer.entities:
            raise SemanticLayerError(
                f"join references unknown entity {join.to_entity!r}"
            )

    return layer


def _split_table(qualified: str) -> tuple[str, str]:
    if "." not in qualified:
        raise SemanticLayerError(
            f"entity.table must be qualified as 'schema.table', got {qualified!r}"
        )
    schema, _, table = qualified.partition(".")
    return schema, table


async def _introspect_columns(
    conn: asyncpg.Connection,
    schemas: Iterable[str],
) -> dict[tuple[str, str], set[str]]:
    """Return {(schema, table): {col, ...}} for every listed schema. Empty
    schemas return an empty dict — the validator will report missing tables
    rather than the absent schema."""
    rows = await conn.fetch(
        """
        select table_schema, table_name, column_name
          from information_schema.columns
         where table_schema = any($1::text[])
        """,
        list(schemas),
    )
    out: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (row["table_schema"], row["table_name"])
        out.setdefault(key, set()).add(row["column_name"])
    return out


def _extract_column_refs(text: str) -> set[tuple[str, str, str]]:
    """Pull `schema.table.column` triples out of free-form SQL text. Used
    against metric `sql_fragment` and join predicates so we can verify each
    reference resolves to a real column."""
    return {
        (m.group(1).lower(), m.group(2).lower(), m.group(3).lower())
        for m in _QUALIFIED_COL_RE.finditer(text)
    }


def _extract_table_refs(text: str) -> set[tuple[str, str]]:
    """Pull bare `schema.table` references (no trailing `.column`) so
    metrics that use subquery aliases still get table-level validation.

    A 3-part name like `crm.orders.total` contains the 2-part substring
    `orders.total` starting at the `o`, which the table regex would
    otherwise mis-match as `(orders, total)`. We mask out any positions
    already claimed by a column-triple match before extracting tables.
    """
    column_spans = [m.span() for m in _QUALIFIED_COL_RE.finditer(text)]

    def overlaps(start: int, end: int) -> bool:
        for cs, ce in column_spans:
            if start < ce and end > cs:
                return True
        return False

    refs: set[tuple[str, str]] = set()
    for m in _QUALIFIED_TABLE_RE.finditer(text):
        if overlaps(*m.span()):
            continue
        refs.add((m.group(1).lower(), m.group(2).lower()))
    return refs


def _validate_against_schema(
    layer: SemanticLayer,
    columns_by_table: dict[tuple[str, str], set[str]],
) -> None:
    """Verify every entity, dimension, metric, and join references real
    columns. Raises SemanticLayerError on the first miss — listing every
    miss at once would bury the actionable message.

    SQL aliases like `o.status` inside a `FROM crm.orders o ...` block look
    syntactically like `schema.column` but the "schema" is the alias. We
    skip any qualified reference whose schema isn't one of the entities'
    schemas (currently just `crm`) — alias resolution is the SQL engine's
    job, not ours.
    """
    known_schemas = {_split_table(e.table)[0] for e in layer.entities.values()}
    # Entities: table must exist; primary_key must be a column on it.
    for entity in layer.entities.values():
        schema, table = _split_table(entity.table)
        cols = columns_by_table.get((schema, table))
        if cols is None:
            raise SemanticLayerError(
                f"entity {entity.name!r} references missing table {entity.table!r}"
            )
        if entity.primary_key not in cols:
            raise SemanticLayerError(
                f"entity {entity.name!r} primary_key {entity.primary_key!r} "
                f"not found in {entity.table}"
            )

    # Dimensions: the column must exist on the entity's table.
    for dim in layer.dimensions.values():
        entity = layer.entities[dim.entity]
        schema, table = _split_table(entity.table)
        cols = columns_by_table.get((schema, table), set())
        if dim.column not in cols:
            raise SemanticLayerError(
                f"dimension {dim.name!r} column {dim.column!r} not found in {entity.table}"
            )

    # Metrics: every qualified column or table reference inside sql_fragment
    # must resolve. Subquery-aliased metrics (e.g. `FROM crm.orders o ... o.total`)
    # exercise the table-only branch; inline-aggregate metrics exercise the
    # column branch. Both branches enforce that the referenced table is also
    # listed in the metric's `entities` so the planner can't sneak past the
    # cross-entity check by hiding tables in a fragment.
    for metric in layer.metrics.values():
        allowed_tables: set[tuple[str, str]] = {
            _split_table(layer.entities[e].table) for e in metric.entities
        }
        column_refs = {
            ref for ref in _extract_column_refs(metric.sql_fragment)
            if ref[0] in known_schemas
        }
        table_refs = {
            ref for ref in _extract_table_refs(metric.sql_fragment)
            if ref[0] in known_schemas
        }
        if not column_refs and not table_refs:
            raise SemanticLayerError(
                f"metric {metric.name!r} sql_fragment has no qualified "
                f"references — use crm.table.column or crm.table aliases "
                f"so the validator can verify them"
            )
        for schema, table, column in column_refs:
            key = (schema, table)
            cols = columns_by_table.get(key)
            if cols is None:
                raise SemanticLayerError(
                    f"metric {metric.name!r} references missing table "
                    f"{schema}.{table}"
                )
            if column not in cols:
                raise SemanticLayerError(
                    f"metric {metric.name!r} references missing column "
                    f"{schema}.{table}.{column}"
                )
            if key not in allowed_tables:
                raise SemanticLayerError(
                    f"metric {metric.name!r} references column {schema}.{table}."
                    f"{column} but {schema}.{table} is not listed in its entities"
                )
        for schema, table in table_refs:
            key = (schema, table)
            if key not in columns_by_table:
                raise SemanticLayerError(
                    f"metric {metric.name!r} references missing table "
                    f"{schema}.{table}"
                )
            if key not in allowed_tables:
                raise SemanticLayerError(
                    f"metric {metric.name!r} references table {schema}.{table} "
                    f"but it is not listed in its entities"
                )

    # Joins: every column referenced in the predicate must exist.
    for join in layer.joins:
        refs = {
            ref for ref in _extract_column_refs(join.predicate)
            if ref[0] in known_schemas
        }
        if not refs:
            raise SemanticLayerError(
                f"join {join.from_entity}->{join.to_entity}: `predicate` must "
                f"reference at least one fully qualified column"
            )
        for schema, table, column in refs:
            cols = columns_by_table.get((schema, table))
            if cols is None:
                raise SemanticLayerError(
                    f"join {join.from_entity}->{join.to_entity} references "
                    f"missing table {schema}.{table}"
                )
            if column not in cols:
                raise SemanticLayerError(
                    f"join {join.from_entity}->{join.to_entity} references "
                    f"missing column {schema}.{table}.{column}"
                )


def _validate_join_paths(layer: SemanticLayer) -> None:
    """Each multi-entity metric must have a join path connecting all of its
    entities — otherwise the compiler in US-030 will fail mid-query. Done in
    pure Python; no DB call.
    """
    # Undirected adjacency from the joins list.
    adjacency: dict[str, set[str]] = {name: set() for name in layer.entities}
    for join in layer.joins:
        adjacency[join.from_entity].add(join.to_entity)
        adjacency[join.to_entity].add(join.from_entity)

    def reachable_from(start: str) -> set[str]:
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for nbr in adjacency[node]:
                if nbr not in seen:
                    seen.add(nbr)
                    stack.append(nbr)
        return seen

    for metric in layer.metrics.values():
        if len(metric.entities) <= 1:
            continue
        head, *rest = metric.entities
        reachable = reachable_from(head)
        for other in rest:
            if other not in reachable:
                raise SemanticLayerError(
                    f"metric {metric.name!r} references entities "
                    f"{metric.entities!r} but no join path connects "
                    f"{head!r} to {other!r}"
                )


def get_database_url() -> str | None:
    """Read `CRM_DATABASE_URL`, falling back to `ANALYTICS_DATABASE_URL` so
    deployments that ran Module 7 keep working until they migrate. None
    disables live validation."""
    for var in ("CRM_DATABASE_URL", "ANALYTICS_DATABASE_URL"):
        raw = os.environ.get(var)
        if raw and raw.strip():
            return raw.strip()
    return None


async def load_and_validate(
    *,
    database_url: str | None = None,
    yaml_path: Path | str | None = None,
) -> SemanticLayer:
    """Full startup validation: parse YAML, introspect the DB, cross-check.

    If `database_url` is None and no env var is set, skips the live check
    and logs a warning — useful for unit tests that don't have a DB. The
    structural checks (entity/dimension/metric refs, parse) always run.
    """
    layer = parse_yaml(yaml_path)
    _validate_join_paths(layer)

    db_url = database_url if database_url is not None else get_database_url()
    if db_url is None:
        log.warning(
            "semantic_layer.live_validation_skipped — set CRM_DATABASE_URL "
            "(or ANALYTICS_DATABASE_URL) to enable column-reference checks"
        )
        return layer

    schemas = {_split_table(e.table)[0] for e in layer.entities.values()}
    conn = await asyncpg.connect(db_url, timeout=10.0)
    try:
        columns_by_table = await _introspect_columns(conn, schemas)
    finally:
        await conn.close()
    _validate_against_schema(layer, columns_by_table)

    log.info(
        "semantic_layer.loaded entities=%d dimensions=%d metrics=%d joins=%d",
        len(layer.entities),
        len(layer.dimensions),
        len(layer.metrics),
        len(layer.joins),
    )
    return layer
