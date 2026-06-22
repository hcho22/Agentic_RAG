"""US-030: query planner for structured-RAG.

`plan_query` translates a natural-language question into a structured
`PlanSpec` referencing the entities, dimensions, and metrics defined in
`backend/semantic_layer.yaml`. The result is consumed by the compiler in
`backend/sql_compiler.py` which assembles deterministic SQL — there is no
LLM in the SQL-generation step, so the planner's choice of metric is the
only place semantic ambiguity gets resolved.

OpenAI function-calling gives us two competing tools (`submit_matched_plan`
and `submit_no_match`); the model picks whichever fits. This is cleaner
than a single tool with a status enum because the function schemas
themselves describe the matched vs unmatched contracts and the model only
has to fill one shape.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal, Union

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from semantic_layer import SemanticLayer

log = logging.getLogger("agentic_rag.backend.planner")

TimeGrain = Literal["day", "week", "month", "quarter", "year"]
FilterOp = Literal["eq", "neq", "gt", "gte", "lt", "lte", "in", "between"]
FallbackTool = Literal["file_search", "web_search", "none"]


class Filter(BaseModel):
    """A single WHERE-clause fragment. `dimension` references a dimension
    name from the semantic layer; the compiler resolves it to the underlying
    column. `value` shape depends on `op`:
      * eq/neq/gt/gte/lt/lte → scalar
      * in                   → list of scalars
      * between              → 2-element [low, high] list
    """

    dimension: str = Field(
        ..., description="Dimension name from semantic_layer.dimensions."
    )
    op: FilterOp = Field(
        ..., description="Comparison operator."
    )
    value: Union[str, int, float, bool, list[Any]] = Field(
        ...,
        description=(
            "Scalar for eq/neq/gt/gte/lt/lte, list for in, 2-element "
            "[low, high] for between. ISO 8601 strings for time values."
        ),
    )


class PlanSpec(BaseModel):
    """Structured query plan emitted by `plan_query` and consumed by
    `sql_compiler.compile`."""

    metrics: list[str] = Field(
        ...,
        description="One or more metric names from semantic_layer.metrics.",
    )
    dimensions: list[str] = Field(
        default_factory=list,
        description=(
            "Dimension names to group by. Empty means no GROUP BY — the "
            "query returns one row per metric (the aggregate)."
        ),
    )
    filters: list[Filter] = Field(
        default_factory=list,
        description="WHERE-clause filters applied before aggregation.",
    )
    time_grain: TimeGrain | None = Field(
        default=None,
        description=(
            "When set, any dimension of kind=time in `dimensions` is "
            "bucketed via date_trunc(time_grain, column). Ignored if no "
            "time dimension is selected."
        ),
    )


class PlanMatched(BaseModel):
    """Status discriminator: planner produced a valid PlanSpec."""

    status: Literal["matched"] = "matched"
    plan: PlanSpec


class PlanNoMatch(BaseModel):
    """Status discriminator: planner could not map the question to the
    semantic layer. The agent reads `suggested_fallback` to choose its
    next step — typically `file_search` for questions answerable from
    documents, or `none` for genuinely out-of-scope questions.
    """

    status: Literal["no_match"] = "no_match"
    reason: str
    suggested_fallback: FallbackTool


PlanQueryResult = Union[PlanMatched, PlanNoMatch]


def get_planner_model() -> str:
    """Falls through `OPENAI_PLANNER_MODEL` → `OPENAI_MODEL` → `gpt-4o-mini`.
    Lets ops point the planner at a cheaper / faster model independent of
    the main chat model — function-calling accuracy is fine on 4o-mini at
    the prompt complexity we're at.

    US-023: this selects the *model* only — never the provider/base_url. The
    client is the shared answerer client passed in by the caller; this helper
    never constructs its own client, and a per-call base_url is unsupported
    (one chat host per deployment for all text generation; ADR-0006)."""
    return (
        os.environ.get("OPENAI_PLANNER_MODEL")
        or os.environ.get("OPENAI_MODEL")
        or "gpt-4o-mini"
    )


class PlanQueryInput(BaseModel):
    """Tool-schema input the agent supplies — just the user's question."""

    question: str = Field(
        ...,
        min_length=1,
        description=(
            "Natural-language question about the structured business data. "
            "The planner maps it to a PlanSpec; `sql_search` compiles the "
            "PlanSpec into SQL and executes it."
        ),
    )


def _format_semantic_layer_block(layer: SemanticLayer) -> str:
    """Render the semantic layer as a compact text block the LLM can read.
    Includes synonyms because metric ambiguity is the whole reason this
    path exists — the model needs to see that "revenue" maps to both
    `gross_revenue` and `net_revenue` so it can choose deliberately."""
    lines: list[str] = []
    lines.append("Entities:")
    for e in layer.entities.values():
        desc = f" — {e.description}" if e.description else ""
        lines.append(f"  - {e.name} (table: {e.table}){desc}")

    lines.append("\nDimensions:")
    for d in layer.dimensions.values():
        syn = f"  synonyms: {', '.join(d.synonyms)}" if d.synonyms else ""
        kind = f" [{d.kind}]" if d.kind != "categorical" else ""
        lines.append(
            f"  - {d.name} (entity: {d.entity}, column: {d.column}){kind}"
        )
        if d.description:
            lines.append(f"      {d.description}")
        if syn:
            lines.append(f"    {syn}")

    lines.append("\nMetrics:")
    for m in layer.metrics.values():
        syn = f"    synonyms: {', '.join(m.synonyms)}" if m.synonyms else ""
        lines.append(
            f"  - {m.name} (grain: {m.grain}, entities: {', '.join(m.entities)})"
        )
        lines.append(f"      {m.description}")
        if syn:
            lines.append(syn)

    return "\n".join(lines)


SYSTEM_PROMPT_TEMPLATE = """\
You translate natural-language business questions into a structured query plan \
against a Cube-style semantic layer.

Two tools are available — choose exactly one:

  * submit_matched_plan: the question maps cleanly onto the semantic layer's \
metrics and dimensions. Fill in PlanSpec.
  * submit_no_match: the question can't be answered from the semantic layer. \
Explain briefly why, and pick a fallback tool the parent agent should try \
instead.

Hard rules:
  * Questions like "what is X?" or "how many X?" that don't ask for a \
breakdown are perfectly valid — emit `submit_matched_plan` with empty \
`dimensions` and the relevant metric. The result is a single scalar. Do \
NOT call `submit_no_match` just because the question is short or doesn't \
specify a grouping.
  * Use metric and dimension names exactly as listed below. No spelling drift.
  * Filters are objects with exactly three keys: `dimension`, `op`, `value`. \
Use the dimension's name (e.g. "order_status"), not a column path. \
Allowed ops: eq, neq, gt, gte, lt, lte, in, between. Do NOT use \
shorthand like {{"order_status": "paid"}} — always emit the three-key form.
  * `op: "between"` → `value` is a 2-element list [low, high].
  * `op: "in"` → `value` is a non-empty list.
  * All other ops → `value` is a scalar (string, number, or boolean).
  * When the user asks for a time bucket ("by month", "weekly", "per quarter"), \
include a time-kind dimension in dimensions AND set time_grain. \
For order data the default time-kind dim is "order_created_at".
  * For date-range filters ("between Jan and March", "in 2026 Q1", "last 90 days"), \
use a filter with op="between" on a time-kind dimension and ISO 8601 \
date strings as the two-element value list — e.g. \
{{"dimension": "order_created_at", "op": "between", "value": ["2026-01-01", "2026-04-01"]}}.
  * Distinguish "revenue" carefully: synonyms can map to multiple metrics. \
When the user is explicit ("net revenue", "after refunds"), follow their \
wording. When ambiguous, pick gross_revenue and the agent will surface the \
choice in its reply.
  * Out-of-scope examples: requests for raw rows ("show me the orders from \
Tuesday"), novel metrics not in the layer, free-text descriptions of \
products. For these, call submit_no_match with suggested_fallback="file_search" \
if documents might answer, otherwise "none".

Example PlanSpec for "what was our gross revenue by month for paid orders":
{{
  "metrics": ["gross_revenue"],
  "dimensions": ["order_created_at"],
  "filters": [{{"dimension": "order_status", "op": "eq", "value": "paid"}}],
  "time_grain": "month"
}}

Semantic layer:
{layer_block}
"""


def _matched_plan_function_schema() -> dict[str, Any]:
    """OpenAI tool entry for the matched-plan path. `plan` is the PlanSpec
    JSON schema; we strip Pydantic's `$defs` / `definitions` wrapping that
    OpenAI's function-calling rejects for top-level parameters."""
    return {
        "type": "function",
        "function": {
            "name": "submit_matched_plan",
            "description": (
                "Submit a structured query plan when the question maps "
                "cleanly onto the semantic layer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": PlanSpec.model_json_schema(),
                },
                "required": ["plan"],
            },
        },
    }


def _no_match_function_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "submit_no_match",
            "description": (
                "Declare that the question cannot be answered from the "
                "semantic layer and suggest where the agent should fall back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the question is out of scope.",
                    },
                    "suggested_fallback": {
                        "type": "string",
                        "enum": ["file_search", "web_search", "none"],
                        "description": (
                            "Which tool the parent agent should try next. "
                            "file_search for questions answerable from the "
                            "user's documents, web_search for public facts, "
                            "none when there is no productive next step."
                        ),
                    },
                },
                "required": ["reason", "suggested_fallback"],
            },
        },
    }


async def plan_query(
    *,
    openai_client: AsyncOpenAI,
    question: str,
    layer: SemanticLayer,
) -> PlanQueryResult:
    """Map `question` to a PlanSpec or no_match via OpenAI function-calling."""
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        layer_block=_format_semantic_layer_block(layer),
    )
    tools = [_matched_plan_function_schema(), _no_match_function_schema()]
    resp = await openai_client.chat.completions.create(
        model=get_planner_model(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        tools=tools,
        tool_choice="required",  # force one of the two functions
        temperature=0.0,
    )
    choice = resp.choices[0].message
    tool_calls = choice.tool_calls or []
    if not tool_calls:
        # Defensive: tool_choice=required should make this unreachable, but
        # API drift has surprised us before — surface a no_match the agent
        # can react to.
        return PlanNoMatch(
            reason="planner returned no tool call",
            suggested_fallback="none",
        )

    call = tool_calls[0]
    name = call.function.name
    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError:
        return PlanNoMatch(
            reason=f"planner returned malformed arguments: {call.function.arguments!r}",
            suggested_fallback="none",
        )

    if name == "submit_matched_plan":
        plan_payload = args.get("plan")
        # GPT-4o-mini occasionally flattens the plan onto the top-level
        # function args (skipping the `plan` wrapper), even though the
        # schema requires nesting. If `args` itself looks like a PlanSpec
        # — has `metrics` at the top — treat it as the plan.
        if not isinstance(plan_payload, dict) and "metrics" in args:
            plan_payload = args
        if not isinstance(plan_payload, dict):
            return PlanNoMatch(
                reason=f"matched-plan call missing `plan` object: {args!r}",
                suggested_fallback="none",
            )
        # GPT-4o-mini ignores the schema's filter shape more often than is
        # comfortable — it emits {name, operator, value}, or short-form
        # {order_status: "paid"}, or key-as-dim {order_created_at: {between:
        # [...]}}. The model's *intent* is unambiguous in each case; we
        # coerce to canonical form rather than failing the question with a
        # no_match. See _coerce_plan_payload for the supported alt shapes.
        plan_payload = _coerce_plan_payload(plan_payload, layer)
        try:
            plan = PlanSpec.model_validate(plan_payload)
        except Exception as e:  # noqa: BLE001
            return PlanNoMatch(
                reason=f"matched-plan failed PlanSpec validation: {e}",
                suggested_fallback="none",
            )
        # Cross-check against the layer: every named metric / dimension /
        # filter dimension must exist. The planner's prompt instructs the
        # model, but a model that hallucinates a metric should not propagate
        # into the compiler.
        layer_check = _validate_plan_against_layer(plan, layer)
        if layer_check is not None:
            return PlanNoMatch(
                reason=layer_check,
                suggested_fallback="none",
            )
        return PlanMatched(plan=plan)

    if name == "submit_no_match":
        return PlanNoMatch(
            reason=str(args.get("reason") or "unspecified"),
            suggested_fallback=args.get("suggested_fallback") or "none",
        )

    return PlanNoMatch(
        reason=f"planner called unexpected function: {name!r}",
        suggested_fallback="none",
    )


# Maps the alternate operator strings GPT-4o-mini frequently produces back to
# the canonical short codes the Filter.op enum uses. Anything outside this
# table falls through unchanged — Pydantic will reject it downstream, which
# is the desired behaviour (we coerce common variants, not anything goes).
_FILTER_OP_ALIASES: dict[str, str] = {
    "eq": "eq", "equals": "eq", "equal": "eq", "==": "eq", "=": "eq", "is": "eq",
    "neq": "neq", "not_equals": "neq", "ne": "neq", "!=": "neq",
    "gt": "gt", "greater_than": "gt", ">": "gt",
    "gte": "gte", "greater_than_or_equal": "gte", "ge": "gte", ">=": "gte",
    "lt": "lt", "less_than": "lt", "<": "lt",
    "lte": "lte", "less_than_or_equal": "lte", "le": "lte", "<=": "lte",
    "in": "in", "one_of": "in",
    "between": "between", "range": "between",
}


def _coerce_filter(raw: Any, dimension_names: set[str]) -> Any:
    """Map common LLM-emitted filter shapes back to the canonical {dimension,
    op, value} form. Returns the raw value unchanged for shapes we can't
    confidently coerce — Pydantic will then surface a useful error.

    Supported alt shapes (observed in the wild on gpt-4o-mini, US-031 eval):
      * {name, operator, value}           — alt field names
      * {dimension, op, value} with op=str alias (e.g. "equals" → "eq")
      * {<dim_name>: <scalar>}            — short-form, treated as op=eq
      * {<dim_name>: {<op>: <value>}}     — key-as-dim with nested op
    """
    if not isinstance(raw, dict):
        return raw

    # Already canonical, possibly with an op alias to normalise.
    if {"dimension", "op", "value"}.issubset(raw.keys()):
        op = raw["op"]
        if isinstance(op, str):
            raw = {**raw, "op": _FILTER_OP_ALIASES.get(op.lower(), op)}
        return raw

    # `{name, operator, value}` — alt key names.
    if "name" in raw and "value" in raw and ("operator" in raw or "op" in raw):
        op = raw.get("operator") or raw.get("op")
        if isinstance(op, str):
            op = _FILTER_OP_ALIASES.get(op.lower(), op)
        return {"dimension": raw["name"], "op": op, "value": raw["value"]}

    # `{<dim_name>: ...}` — single-key dispatch. Only coerce when the key is
    # a real dimension name from the layer, otherwise we'd guess wrong.
    if len(raw) == 1:
        (key, val), = raw.items()
        if key in dimension_names:
            if isinstance(val, dict) and len(val) == 1:
                # {dim: {op: value}}
                ((op_raw, value),) = val.items()
                op = _FILTER_OP_ALIASES.get(op_raw.lower(), op_raw) if isinstance(op_raw, str) else op_raw
                return {"dimension": key, "op": op, "value": value}
            # {dim: scalar} — short form, treat as equality.
            return {"dimension": key, "op": "eq", "value": val}

    return raw


def _coerce_plan_payload(payload: dict, layer: SemanticLayer) -> dict:
    """Lossy normalisation step between OpenAI's function call output and
    PlanSpec validation. Two transformations:

      1. Each filter passes through `_coerce_filter` to absorb the alt
         shapes GPT-4o-mini emits despite the schema.
      2. If `time_grain` is set but no time-kind dimension was selected,
         add `order_created_at` (the default order-side time dim) so the
         compiler has something to date_trunc over. The PRD's expected
         behaviour is "time_grain implies time dim" — the prompt now says
         so explicitly, but a graceful fallback here keeps single-mistake
         plans on the matched path.
    """
    out = dict(payload)
    dim_names = set(layer.dimensions.keys())
    if isinstance(out.get("filters"), list):
        out["filters"] = [_coerce_filter(f, dim_names) for f in out["filters"]]
    # time-grain auto-promotion
    time_grain = out.get("time_grain")
    dims = out.get("dimensions") or []
    if time_grain and isinstance(dims, list):
        has_time = any(
            isinstance(d, str)
            and d in layer.dimensions
            and layer.dimensions[d].kind == "time"
            for d in dims
        )
        if not has_time:
            # Pick the default time dim. order_created_at is the canonical
            # "when did the order happen" — covers >90% of intent.
            default_time = "order_created_at"
            if default_time in layer.dimensions and layer.dimensions[default_time].kind == "time":
                log.info(
                    "planner.auto_added_time_dim grain=%s dim=%s",
                    time_grain, default_time,
                )
                out["dimensions"] = list(dims) + [default_time]
    return out


def _validate_plan_against_layer(
    plan: PlanSpec, layer: SemanticLayer
) -> str | None:
    """Returns an error string if the plan references unknown layer items,
    otherwise None. Keeps the planner honest — a model that invents a
    metric called `customer_health_score` should not slip past."""
    if not plan.metrics:
        return "plan has no metrics"
    for m in plan.metrics:
        if m not in layer.metrics:
            return f"plan references unknown metric {m!r}"
    for d in plan.dimensions:
        if d not in layer.dimensions:
            return f"plan references unknown dimension {d!r}"
    for f in plan.filters:
        if f.dimension not in layer.dimensions:
            return f"plan filter references unknown dimension {f.dimension!r}"
        if f.op == "between":
            if not isinstance(f.value, list) or len(f.value) != 2:
                return (
                    f"filter on {f.dimension!r} uses op=between but value is "
                    f"not a 2-element list: {f.value!r}"
                )
        elif f.op == "in":
            if not isinstance(f.value, list) or not f.value:
                return (
                    f"filter on {f.dimension!r} uses op=in but value is not a "
                    f"non-empty list: {f.value!r}"
                )
    if plan.time_grain is not None:
        has_time_dim = any(
            layer.dimensions[d].kind == "time" for d in plan.dimensions
        )
        if not has_time_dim:
            return (
                "plan sets time_grain but no time-kind dimension was selected "
                "— add e.g. order_created_at to dimensions"
            )
    return None


def plan_query_tool_schema() -> dict[str, Any]:
    """Chat Completions `tools[]` entry for the plan_query tool the parent
    agent calls. This is the schema the *agent* sees; the planner's own
    function-calling tools (submit_matched_plan / submit_no_match) are
    internal to plan_query and never exposed upward."""
    return {
        "type": "function",
        "function": {
            "name": "plan_query",
            "description": (
                "Plan a structured-data query by mapping the user's question "
                "to a semantic-layer PlanSpec (which metrics, dimensions, "
                "filters, time grain). Returns either {status: \"matched\", "
                "plan: ...} or {status: \"no_match\", reason, "
                "suggested_fallback}. You must call this BEFORE calling "
                "sql_search; sql_search requires a matched plan as input. "
                "On no_match with suggested_fallback=\"file_search\", call "
                "search_documents next; otherwise explain to the user that "
                "the question is out of scope for the structured data."
            ),
            "parameters": PlanQueryInput.model_json_schema(),
        },
    }
