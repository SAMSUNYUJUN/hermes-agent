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
        })
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return tool_error(f"Failed to record DTC site exploration: {exc}", success=False)


DTC_SITE_SEARCH_CONTEXT_SCHEMA = {
    "name": "dtc_site_search_context",
    "description": (
        "Check whether a learned site-specific DTC search skill exists for an "
        "independent-site URL. If the result has has_skill=true, immediately "
        "call skill_view(name=skill_view_name) before browsing; do not ask this "
        "tool for the skill body. The site skill summarizes the efficient "
        "search path for that website and may direct you to a canonical redirect "
        "target, catalog host, category page, or product listing URL instead of "
        "the original user-provided URL. Follow it so you can skip unnecessary "
        "exploratory snapshots, dead-end UI paths, and poor search patterns. "
        "This tool does not judge same-item matches. Do not start with "
        "Google/web_search unless direct site exploration fails or reveals a "
        "related catalog domain."
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
        session_id=kw.get("session_id", ""),
        tool_call_id=kw.get("tool_call_id", ""),
    ),
    emoji="🧭",
)
