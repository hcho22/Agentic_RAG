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
    the prompt complexity we're at."""
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
  * Use metric and dimension names exactly as listed below. No spelling drift.
  * Filters must reference a dimension by name (not a raw column).
  * When the user asks for a time bucket ("by month", "weekly", "per quarter"), \
include a time-kind dimension in dimensions AND set time_grain. Without a \
time-kind dimension, time_grain is ignored.
  * Distinguish "revenue" carefully: synonyms can map to multiple metrics. \
When the user is explicit ("net revenue", "after refunds"), follow their \
wording. When ambiguous, pick gross_revenue and the agent will surface the \
choice in its reply.
  * Out-of-scope examples: requests for raw rows ("show me the orders from \
Tuesday"), novel metrics not in the layer, free-text descriptions of \
products. For these, call submit_no_match with suggested_fallback="file_search" \
if documents might answer, otherwise "none".

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
        if not isinstance(plan_payload, dict):
            return PlanNoMatch(
                reason=f"matched-plan call missing `plan` object: {args!r}",
                suggested_fallback="none",
            )
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
