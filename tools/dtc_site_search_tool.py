"""Tools for recording DTC independent-site product-search explorations."""

import json
from typing import Any, Dict, List

from tools.registry import registry, tool_error


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def dtc_site_search_context(site_url: str) -> str:
    """Return any learned site-specific search skill for a DTC site."""
    if not site_url:
        return tool_error("site_url is required", success=False)
    try:
        from agent.dtc_site_search_learner import get_site_skill_context

        return json.dumps(get_site_skill_context(site_url), ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"Failed to load DTC site-search context: {exc}", success=False)


def dtc_site_search_record(
    site_url: str,
    sku_id: str,
    success: bool,
    exploration_summary: str = "",
    successful_steps: str = "",
    pitfalls: List[Any] = None,
    product_urls: List[Any] = None,
    candidate_products: List[Any] = None,
    session_id: str = "",
    tool_call_id: str = "",
    route_used: str = "",
    tool_version: str = "",
    tool_success: bool = False,
    tool_failure_event_id: str = "",
    api_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    tool_call_count: int = 0,
    elapsed_seconds: float = 0.0,
) -> str:
    """Record one completed DTC-site exploration and start background cleanup."""
    if not site_url:
        return tool_error("site_url is required", success=False)
    if not sku_id:
        return tool_error("sku_id is required", success=False)
    try:
        from agent.dtc_site_search_learner import record_exploration

        result = record_exploration({
            "site_url": site_url,
            "sku_id": str(sku_id),
            "success": bool(success),
            "exploration_summary": exploration_summary or "",
            "successful_steps": successful_steps or "",
            "pitfalls": _as_list(pitfalls),
            "product_urls": _as_list(product_urls),
            "candidate_products": _as_list(candidate_products),
            "session_id": session_id or "",
            "tool_call_id": tool_call_id or "",
            "route_used": route_used or "",
            "tool_version": tool_version or "",
            "tool_success": bool(tool_success),
            "tool_failure_event_id": tool_failure_event_id or "",
            "api_calls": int(api_calls or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "total_tokens": int(total_tokens or 0),
            "tool_call_count": int(tool_call_count or 0),
            "elapsed_seconds": float(elapsed_seconds or 0.0),
        })
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"Failed to record DTC site exploration: {exc}", success=False)


def dtc_site_search_tool(
    site_url: str,
    query: str,
    expected_terms: List[Any] = None,
    max_candidates: int = 5,
    session_id: str = "",
    tool_call_id: str = "",
) -> str:
    """Run an enabled generated site-specific DTC search tool."""
    if not site_url:
        return tool_error("site_url is required", success=False)
    if not query:
        return tool_error("query is required", success=False)
    try:
        from agent.dtc_site_search_learner import run_generated_site_tool

        result = run_generated_site_tool(
            site_url=site_url,
            query=query,
            expected_terms=[str(x) for x in _as_list(expected_terms) if str(x).strip()],
            max_candidates=max_candidates,
            session_id=session_id,
            tool_call_id=tool_call_id,
        )
        if not result.get("success"):
            result.setdefault(
                "fallback_instruction",
                (
                    "Generated DTC site tool failed or is unavailable. Fall back to "
                    "dtc_site_search_context and the learned skill if present, then "
                    "record the successful fallback exploration with dtc_site_search_record."
                ),
            )
        else:
            result.setdefault(
                "completion_instruction",
                (
                    "Generated DTC site tool succeeded. Use these structured candidates "
                    "and evidence to decide the answer, then record this exploration with "
                    "dtc_site_search_record(tool_success=true, route_used='tool_first', "
                    "tool_version=<returned tool_version>). Do not load the site skill "
                    "after a successful generated-tool call."
                ),
            )
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"Failed to run generated DTC site-search tool: {exc}", success=False)


DTC_SITE_SEARCH_CONTEXT_SCHEMA = {
    "name": "dtc_site_search_context",
    "description": (
        "Check whether a learned site-specific DTC search skill exists for an "
        "independent-site URL and whether a generated site tool is available. "
        "If the result has has_tool=true, call dtc_site_search_tool first. If "
        "that generated tool returns success=true, do not load the site skill; "
        "use the returned candidates/evidence to answer or say no match. Only if "
        "the generated tool returns success=false and the "
        "result has has_skill=true, call skill_view(name=skill_view_name) before "
        "browsing; do not ask this tool for the skill body. The site skill "
        "summarizes the efficient search path for that website and may direct "
        "you to a canonical redirect target, catalog host, category page, or "
        "product listing URL instead of the original user-provided URL. Follow "
        "it so you can skip unnecessary exploratory snapshots, dead-end UI "
        "paths, and poor search patterns. This tool does not judge same-item "
        "matches. Do not start with Google/web_search unless direct site "
        "exploration fails or reveals a related catalog domain."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "site_url": {
                "type": "string",
                "description": "The independent-site URL/domain the user wants searched.",
            },
        },
        "required": ["site_url"],
    },
}


DTC_SITE_SEARCH_TOOL_SCHEMA = {
    "name": "dtc_site_search_tool",
    "description": (
        "Run the generated site-specific DTC product-search tool for an "
        "independent-site URL. Use this first when dtc_site_search_context "
        "returns has_tool=true. It returns structured product candidates and "
        "evidence without loading the site skill. If it returns success=true, "
        "do not load the site skill even when candidates are weak; use the "
        "returned evidence to answer or say no match. Record the "
        "exploration with dtc_site_search_record(tool_success=true, "
        "route_used='tool_first', tool_version=<returned tool_version>). If it returns "
        "success=false, fall back to the learned site skill and then record "
        "the fallback exploration with dtc_site_search_record(route_used='skill_only', "
        "tool_failure_event_id=<returned failure.failure_event_id>) so the background "
        "learner can repair the generated script."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "site_url": {
                "type": "string",
                "description": "The DTC independent-site URL/domain the user wants searched.",
            },
            "query": {
                "type": "string",
                "description": "The target product name, model, SKU title, or concise search query.",
            },
            "expected_terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Distinctive terms expected in a true candidate, if known.",
            },
            "max_candidates": {
                "type": "integer",
                "description": "Maximum number of candidate products to return.",
                "default": 5,
            },
        },
        "required": ["site_url", "query"],
    },
}


DTC_SITE_SEARCH_RECORD_SCHEMA = {
    "name": "dtc_site_search_record",
    "description": (
        "Call this exactly once after completing a full exploration of one DTC "
        "independent site for one TikTok sku_id. It records the search path by "
        "site URL and starts a background LLM cleanup pass. Use it to preserve "
        "successful navigation/search chains, candidate product URLs, and "
        "pitfalls. Do not use it for price comparison or final same-item judgment. "
        "If the user asks for same-item judgment and both TikTok and site-side "
        "images exist, use vision before judging; skip visual comparison when "
        "either side has no images."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "site_url": {"type": "string", "description": "The DTC independent-site URL explored."},
            "sku_id": {"type": "string", "description": "The TikTok SKU id used as the search target."},
            "success": {
                "type": "boolean",
                "description": "True only if the site exploration produced a coherent reusable search chain.",
            },
            "exploration_summary": {
                "type": "string",
                "description": "Concise account of what was explored and what happened.",
            },
            "successful_steps": {
                "type": "string",
                "description": "Ordered successful browsing/search chain, including search terms and page URLs.",
            },
            "pitfalls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Dead ends, misleading UI, blocked routes, fragile selectors, or failed search terms.",
            },
            "product_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Product or collection URLs inspected during the exploration.",
            },
            "candidate_products": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Structured candidate product notes, if any, without judging final match.",
            },
            "route_used": {
                "type": "string",
                "description": "Route that produced this record, e.g. tool_first or skill_only.",
            },
            "tool_version": {
                "type": "string",
                "description": "Generated tool version used when route_used=tool_first.",
            },
            "tool_success": {
                "type": "boolean",
                "description": "True when the active generated tool itself returned the successful candidates. Tool successes do not advance skill review X/Y counters.",
            },
            "tool_failure_event_id": {
                "type": "string",
                "description": "Failure event id returned by dtc_site_search_tool when skill fallback succeeded after tool failure.",
            },
            "api_calls": {"type": "integer", "description": "Optional original search API call count for A/B baseline metrics."},
            "input_tokens": {"type": "integer", "description": "Optional original search input token count for A/B baseline metrics."},
            "output_tokens": {"type": "integer", "description": "Optional original search output token count for A/B baseline metrics."},
            "total_tokens": {"type": "integer", "description": "Optional original search total token count for A/B baseline metrics."},
            "tool_call_count": {"type": "integer", "description": "Optional original search tool call count for A/B baseline metrics."},
            "elapsed_seconds": {"type": "number", "description": "Optional original search elapsed seconds for A/B baseline metrics."},
        },
        "required": ["site_url", "sku_id", "success", "exploration_summary"],
    },
}


registry.register(
    name="dtc_site_search_context",
    toolset="dtc_site_search",
    schema=DTC_SITE_SEARCH_CONTEXT_SCHEMA,
    handler=lambda args, **kw: dtc_site_search_context(site_url=args.get("site_url", "")),
    emoji="🔎",
)

registry.register(
    name="dtc_site_search_tool",
    toolset="dtc_site_search",
    schema=DTC_SITE_SEARCH_TOOL_SCHEMA,
    handler=lambda args, **kw: dtc_site_search_tool(
        site_url=args.get("site_url", ""),
        query=args.get("query", ""),
        expected_terms=args.get("expected_terms"),
        max_candidates=args.get("max_candidates", 5),
        session_id=kw.get("session_id", ""),
        tool_call_id=kw.get("tool_call_id", ""),
    ),
    emoji="🛠️",
)

registry.register(
    name="dtc_site_search_record",
    toolset="dtc_site_search",
    schema=DTC_SITE_SEARCH_RECORD_SCHEMA,
    handler=lambda args, **kw: dtc_site_search_record(
        site_url=args.get("site_url", ""),
        sku_id=args.get("sku_id", ""),
        success=bool(args.get("success", False)),
        exploration_summary=args.get("exploration_summary", ""),
        successful_steps=args.get("successful_steps", ""),
        pitfalls=args.get("pitfalls"),
        product_urls=args.get("product_urls"),
        candidate_products=args.get("candidate_products"),
        route_used=args.get("route_used", ""),
        tool_version=args.get("tool_version", ""),
        tool_success=bool(args.get("tool_success", False)),
        tool_failure_event_id=args.get("tool_failure_event_id", ""),
        api_calls=args.get("api_calls", 0),
        input_tokens=args.get("input_tokens", 0),
        output_tokens=args.get("output_tokens", 0),
        total_tokens=args.get("total_tokens", 0),
        tool_call_count=args.get("tool_call_count", 0),
        elapsed_seconds=args.get("elapsed_seconds", 0.0),
        session_id=kw.get("session_id", ""),
        tool_call_id=kw.get("tool_call_id", ""),
    ),
    emoji="🧭",
)
