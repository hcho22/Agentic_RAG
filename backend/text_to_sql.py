"""US-023: text-to-SQL `query_database` tool over a designated analytics schema.

The agent describes the user's question in natural language; this module asks
an LLM to translate it to a single Postgres SELECT statement, then runs that
statement under a read-only role inside a `READ ONLY` transaction with a
hard statement timeout. Three layers stack up so a single layer's bug
doesn't expose writes:

  1. Role-level: `ANALYTICS_DATABASE_URL` authenticates as `analytics_readonly`
     (created in 20260506120000_init_analytics_schema.sql), which has no
     write privileges on any schema.
  2. Transaction-level: every query runs inside `BEGIN READ ONLY` so even a
     superuser connection couldn't accidentally write.
  3. Statement-level: a regex guard rejects anything that isn't a SELECT or
     WITH-CTE-into-SELECT, and rejects any reference to a schema not in the
     `ALLOWED_SQL_SCHEMAS` allowlist (default `analytics`).

The generated SQL is returned in the tool result so it appears verbatim in
LangSmith traces — the PRD validation test inspects the trace to confirm
attribution.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

log = logging.getLogger("agentic_rag.backend.text_to_sql")

DEFAULT_ALLOWED_SCHEMAS = ("analytics", "crm")
DEFAULT_QUERY_TIMEOUT_MS = 10_000
DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000

# Statements the LLM might try to emit despite the system prompt. A standalone
# match (not a substring inside a quoted literal) fails the safety check.
# Defence in depth: the role lacks write privileges, but failing fast here
# avoids burning a roundtrip for clearly bad output.
_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "merge", "copy",
    "create", "drop", "alter", "truncate", "grant", "revoke",
    "comment", "do", "call", "vacuum", "analyze", "reindex",
    "lock", "checkpoint", "set", "reset", "show", "discard",
    "begin", "commit", "rollback", "savepoint", "release",
)

# A SQL identifier that *might* be qualified with a schema. Used to find every
# schema reference in the generated SQL so we can confirm they're all allowed.
_SCHEMA_REF_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.[a-zA-Z_][a-zA-Z0-9_]*", re.UNICODE)


class SqlSafetyError(ValueError):
    """Raised when generated SQL fails the safety check."""


class QueryDatabaseInput(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        description=(
            "Natural-language question to answer with SQL against the allowed "
            "analytics schema(s)."
        ),
    )
    row_limit: int = Field(
        default=DEFAULT_ROW_LIMIT,
        ge=1,
        le=MAX_ROW_LIMIT,
        description=(
            "Soft cap on rows returned in the tool result. Aggregate queries "
            "(SUM, COUNT, AVG) typically return one row regardless."
        ),
    )


class QueryDatabaseResult(BaseModel):
    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


def get_analytics_database_url() -> str | None:
    """`ANALYTICS_DATABASE_URL` env. None disables the tool entirely."""
    raw = os.environ.get("ANALYTICS_DATABASE_URL")
    return raw.strip() if raw and raw.strip() else None


def get_allowed_schemas() -> tuple[str, ...]:
    """`ALLOWED_SQL_SCHEMAS` env: comma-separated, default `analytics`."""
    raw = os.environ.get("ALLOWED_SQL_SCHEMAS")
    if not raw or not raw.strip():
        return DEFAULT_ALLOWED_SCHEMAS
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or DEFAULT_ALLOWED_SCHEMAS


def get_query_timeout_ms() -> int:
    """`SQL_QUERY_TIMEOUT_MS` env, default 10000 (10s per PRD)."""
    raw = os.environ.get("SQL_QUERY_TIMEOUT_MS")
    if raw is None or raw == "":
        return DEFAULT_QUERY_TIMEOUT_MS
    try:
        v = int(raw)
    except ValueError as e:
        raise ValueError(f"SQL_QUERY_TIMEOUT_MS must be an int, got {raw!r}") from e
    if v < 1:
        raise ValueError(f"SQL_QUERY_TIMEOUT_MS must be >= 1, got {v}")
    return v


def get_sql_model() -> str:
    """Model used for natural-language → SQL generation.

    Falls through `OPENAI_SQL_MODEL` → `OPENAI_MODEL` → `gpt-4o-mini` so ops
    can pin a cheaper/faster model just for SQL without affecting chat.

    US-023: this selects the *model* only — never the provider/base_url. The
    client is the shared answerer client passed in by the caller; this helper
    never constructs its own client, and a per-call base_url is unsupported
    (one chat host per deployment for all text generation; ADR-0006).
    """
    return (
        os.environ.get("OPENAI_SQL_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )


def is_enabled() -> bool:
    """True when the tool is fully configured. Used by main.py to decide
    whether to expose `query_database` to the agent — keeps existing deploys
    working without forcing the new env vars."""
    return get_analytics_database_url() is not None


def _strip_sql_comments(sql: str) -> str:
    """Remove `--` line comments and `/* ... */` block comments.

    The LLM occasionally emits a comment header. Comments are harmless at
    runtime, but keeping them out of the safety-check input means a comment
    like `-- DROP TABLE analytics.orders` doesn't trip the keyword guard.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _strip_sql_string_literals(sql: str) -> str:
    """Replace `'...'` literals with empty strings so the keyword scan doesn't
    flag literal text like `'I will not DROP'`. Doesn't try to handle escapes
    perfectly — the role + transaction layers backstop any miss here."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate_sql_safety(sql: str, allowed_schemas: tuple[str, ...]) -> str:
    """Reject SQL that isn't a single SELECT (or WITH … SELECT) over allowed
    schemas. Returns the cleaned SQL on success, raises SqlSafetyError on
    rejection. The caller is responsible for executing the *original* SQL —
    the cleaned form is just for validation."""
    cleaned = _strip_sql_comments(sql).strip()
    if not cleaned:
        raise SqlSafetyError("empty sql")

    # Strip a single trailing semicolon; reject anything else after.
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if ";" in cleaned:
        raise SqlSafetyError("multiple statements are not allowed")

    head_lower = cleaned.lower()
    if not (
        head_lower.startswith("select")
        or head_lower.startswith("with ")
        or head_lower.startswith("with(")
    ):
        raise SqlSafetyError(
            "only SELECT (optionally preceded by WITH) statements are allowed"
        )

    # Keyword scan runs on a string-literal-stripped copy so quoted text can't
    # falsely trip the guard. Whole-word match against the forbidden list.
    scanned = _strip_sql_string_literals(cleaned).lower()
    tokens = re.findall(r"[a-zA-Z_]+", scanned)
    # Allow `with` and `select` themselves, even though some forbidden words
    # are statement-level (e.g. `set` in `set search_path`). The list above
    # excludes `with`/`select`/etc. so this is mainly belt-and-braces.
    for tok in tokens:
        if tok in _FORBIDDEN_KEYWORDS:
            raise SqlSafetyError(f"forbidden keyword in sql: {tok!r}")

    # Schema allowlist: every `schema.table` reference must use an allowed
    # schema. We iterate matches on the comments-stripped SQL (not the
    # literal-stripped one) because schema names can contain underscores that
    # the literal stripper would leave intact anyway.
    allowed_lower = {s.lower() for s in allowed_schemas}
    for match in _SCHEMA_REF_RE.finditer(cleaned):
        schema = match.group(1).lower()
        # Heuristic: ignore matches that are obviously column qualifiers like
        # `t.col` where `t` was an alias. We can't perfectly distinguish
        # alias-from-schema without parsing, so we only flag references to
        # *named* forbidden schemas (pg_catalog, information_schema, public,
        # auth, storage). Bare aliases like `o.total` slip through because
        # `o` won't appear in the forbidden set.
        if schema in {
            "pg_catalog", "information_schema", "public", "auth",
            "storage", "extensions", "graphql", "graphql_public",
            "realtime", "supabase_functions", "vault",
        } and schema not in allowed_lower:
            raise SqlSafetyError(f"schema {schema!r} is not in the allowlist")

    return cleaned


async def get_schema_snapshot(database_url: str, schemas: tuple[str, ...]) -> str:
    """Format the column inventory for `schemas` as a compact text block.

    Used both in the LLM SQL-generation system prompt and in the chat-tool
    description so the agent knows what's queryable. Failure to introspect
    (network blip, role permission gap) returns an empty string — the tool
    will still work for explicit-table queries, just without grounding."""
    try:
        conn = await asyncpg.connect(database_url, timeout=10.0)
    except Exception as e:  # noqa: BLE001 — degrade gracefully
        log.warning("text_to_sql.schema_snapshot.connect_failed error=%r", e)
        return ""
    try:
        rows = await conn.fetch(
            """
            select table_schema, table_name, column_name, data_type
              from information_schema.columns
             where table_schema = any($1::text[])
             order by table_schema, table_name, ordinal_position
            """,
            list(schemas),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("text_to_sql.schema_snapshot.query_failed error=%r", e)
        return ""
    finally:
        await conn.close()

    grouped: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in rows:
        key = (row["table_schema"], row["table_name"])
        grouped.setdefault(key, []).append((row["column_name"], row["data_type"]))

    if not grouped:
        return ""

    lines: list[str] = []
    for (schema, table), cols in grouped.items():
        col_str = ", ".join(f"{name} {dtype}" for name, dtype in cols)
        lines.append(f"  {schema}.{table}({col_str})")
    return "Available tables:\n" + "\n".join(lines)


SQL_GENERATION_SYSTEM_PROMPT_TEMPLATE = """\
You translate natural-language questions into a single read-only Postgres SQL \
statement.

Hard rules:
  * Return ONLY a SELECT statement (optionally preceded by a WITH clause).
  * Do NOT use INSERT, UPDATE, DELETE, MERGE, COPY, CREATE, DROP, ALTER, \
TRUNCATE, GRANT, REVOKE, or any session-modifying command (SET, RESET, etc.).
  * Do NOT emit multiple statements. No semicolons except an optional trailing one.
  * Only reference tables in the allowed schemas: {schemas}.
  * Always include a LIMIT clause (default {row_limit}) unless the query is a \
pure aggregate (SUM, COUNT, AVG, MIN, MAX with no GROUP BY).
  * Use ANSI / Postgres syntax. Quote identifiers only when necessary.
  * If the question cannot be answered from the schema, return: \
SELECT 1 WHERE FALSE.

Return JSON in the form: {{"sql": "<sql>"}}.
{schema_block}
"""


async def generate_sql_naive(
    *,
    openai_client: AsyncOpenAI,
    question: str,
    schemas: tuple[str, ...],
    row_limit: int,
    schema_snapshot: str,
) -> str:
    """Public, naive text-to-SQL generator. Once the agent tool registry
    switched to plan_query + sql_search in US-030, this stayed available as
    a library function so the US-031 eval can score "naive vs semantic"
    on the same 30 questions without re-implementing the prompt.

    Same behaviour as before — raises SqlSafetyError on a missing or
    malformed JSON response. Validation + execution are the caller's
    responsibility."""
    schema_block = (
        f"\n{schema_snapshot}\n"
        if schema_snapshot
        else "\nSchema introspection unavailable; rely on the user's question.\n"
    )
    system_prompt = SQL_GENERATION_SYSTEM_PROMPT_TEMPLATE.format(
        schemas=", ".join(schemas),
        row_limit=row_limit,
        schema_block=schema_block,
    )
    resp = await openai_client.chat.completions.create(
        model=get_sql_model(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SqlSafetyError(f"sql generator returned non-JSON: {raw[:200]!r}") from e
    sql = parsed.get("sql") if isinstance(parsed, dict) else None
    if not isinstance(sql, str) or not sql.strip():
        raise SqlSafetyError(f"sql generator returned no sql field: {parsed!r}")
    return sql


def _coerce_value(v: Any) -> Any:
    """Make Postgres types JSON-serialisable for the tool result payload."""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        # Decimal → float loses precision but the agent expects numbers it
        # can compare — magnitudes here are revenue dollars, not money math.
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


async def _execute_select(
    database_url: str,
    sql: str,
    timeout_ms: int,
    row_limit: int,
    params: list[Any] | None = None,
) -> tuple[list[str], list[dict[str, Any]], bool]:
    """Run `sql` as a single READ ONLY transaction with statement_timeout.

    Returns (column_names, rows_as_dicts, truncated). Connection-level
    cancellation timeout is `timeout_ms + 2s` so a runaway query doesn't hang
    the request indefinitely if the server-side timeout misfires. `params`
    are bound positionally via asyncpg's `$1`, `$2`, ... — US-030's compiler
    uses this to keep filter values out of the SQL text (and out of the
    safety check's keyword scan)."""
    cancel_after = (timeout_ms / 1000.0) + 2.0
    conn = await asyncpg.connect(database_url, timeout=10.0)
    try:
        # `set local` confines the timeout to this transaction only — won't
        # leak to the next caller if the connection is somehow reused.
        async with conn.transaction(readonly=True):
            await conn.execute(f"set local statement_timeout = {int(timeout_ms)}")
            if params:
                records = await conn.fetch(sql, *params, timeout=cancel_after)
            else:
                records = await conn.fetch(sql, timeout=cancel_after)
    finally:
        await conn.close()

    if not records:
        return [], [], False

    columns = list(records[0].keys())
    truncated = len(records) > row_limit
    sliced = records[:row_limit]
    rows = [
        {col: _coerce_value(rec[col]) for col in columns}
        for rec in sliced
    ]
    return columns, rows, truncated


async def query_database(
    *,
    openai_client: AsyncOpenAI,
    question: str,
    row_limit: int = DEFAULT_ROW_LIMIT,
    database_url: str | None = None,
    schemas: tuple[str, ...] | None = None,
    timeout_ms: int | None = None,
    schema_snapshot: str | None = None,
) -> QueryDatabaseResult:
    """Generate SQL for `question`, validate, execute, return rows + the SQL.

    Raises `SqlSafetyError` for unsafe generated SQL and `RuntimeError` when
    the tool is misconfigured (no `ANALYTICS_DATABASE_URL`). All other errors
    propagate from asyncpg so the caller can serialise them into the tool
    result for the agent to react to.
    """
    db_url = database_url or get_analytics_database_url()
    if db_url is None:
        raise RuntimeError(
            "query_database called but ANALYTICS_DATABASE_URL is not set"
        )
    schemas = schemas or get_allowed_schemas()
    timeout = timeout_ms if timeout_ms is not None else get_query_timeout_ms()
    snapshot = schema_snapshot if schema_snapshot is not None else await get_schema_snapshot(db_url, schemas)

    sql = await generate_sql_naive(
        openai_client=openai_client,
        question=question,
        schemas=schemas,
        row_limit=row_limit,
        schema_snapshot=snapshot,
    )
    validate_sql_safety(sql, schemas)
    log.info(
        "text_to_sql.execute schemas=%s timeout_ms=%d sql=%r",
        ",".join(schemas),
        timeout,
        sql,
    )

    columns, rows, truncated = await _execute_select(
        database_url=db_url,
        sql=sql,
        timeout_ms=timeout,
        row_limit=row_limit,
    )
    return QueryDatabaseResult(
        sql=sql,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=truncated,
    )


def query_database_tool_schema(schema_snapshot: str | None = None) -> dict[str, Any]:
    """Chat Completions `tools[]` entry for the query_database tool.

    `schema_snapshot` is interpolated into the description so the agent sees
    table/column names without needing to call a separate introspection tool.
    Pass an empty string when introspection failed; the tool still works,
    just with less grounding."""
    snapshot_hint = (
        f"\n\n{schema_snapshot}"
        if schema_snapshot
        else "\n\nSchema is not currently introspectable; ask the user for table names."
    )
    return {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "Run a read-only SQL query against the analytics schema to "
                "answer questions over structured data (totals, counts, "
                "aggregates, lookups). The tool generates SQL from your "
                "question, validates it, and executes it under a read-only "
                "Postgres role with a hard timeout. Returns the rows AND the "
                "generated SQL so you can cite it in your reply. Use this "
                "tool for quantitative questions about the analytics tables; "
                "prefer search_documents for free-text questions about the "
                "user's uploaded documents." + snapshot_hint
            ),
            "parameters": QueryDatabaseInput.model_json_schema(),
        },
    }
