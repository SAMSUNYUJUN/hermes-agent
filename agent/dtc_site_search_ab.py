"""DTC site-search stateless run and A/B evaluation helpers."""

from __future__ import annotations

import concurrent.futures
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse, urlunparse

URL_RE = re.compile(r"https?://[^\s<>)\"']+", re.IGNORECASE)


@dataclass
class DtcSiteSearchAbCase:
    index: int
    sku_id: str
    domain: str
    prompt: str
    expected: str = ""
    product_id: str = ""


def norm_url(url: str) -> str:
    url = (url or "").strip().rstrip(".,)")
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        parsed = urlparse("https://" + url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def extract_urls(text: str) -> List[str]:
    seen = set()
    urls: List[str] = []
    for match in URL_RE.findall(text or ""):
        norm = norm_url(match)
        if norm and norm not in seen:
            seen.add(norm)
            urls.append(norm)
    return urls


def expected_status(expected: str) -> str:
    value = (expected or "").strip()
    if not value:
        return "unknown"
    if value in {"无", "none", "None", "NO", "no", "not found"}:
        return "none"
    return "url"


def score_response(final_response: str, expected: str) -> Dict[str, Any]:
    status = expected_status(expected)
    urls = extract_urls(final_response)
    expected_norm = norm_url(expected) if status == "url" else ""
    if status == "unknown":
        passed: Optional[bool] = None
        reason = "no expected label"
    elif status == "none":
        passed = len(urls) == 0 or bool(re.search(r"\b(no|not found|not a match|没有|无)\b", final_response or "", re.I))
        reason = "expected no candidate"
    else:
        response_text_norm = (final_response or "").replace("www.", "")
        passed = expected_norm in urls or expected_norm in response_text_norm
        reason = "expected URL present" if passed else "expected URL missing"
    return {
        "expected_status": status,
        "expected_url": expected_norm,
        "response_urls": urls,
        "pass": passed,
        "reason": reason,
    }


def build_mode_prompt(case: DtcSiteSearchAbCase, mode: str) -> str:
    common = (
        "This is one stateless DTC same/similar-product search case. "
        "Do not rely on any prior conversation. Return the final candidate URL "
        "if a same/similar product is found; otherwise say no matching product "
        "was found. Always call dtc_site_search_record exactly once after the "
        "site exploration is complete.\n\n"
    )
    if mode == "candidate":
        policy = (
            "A/B mode: candidate. Use production rollout behavior: after "
            "tiktok_sku_lookup, call dtc_site_search_context. If has_tool=true, "
            "call dtc_site_search_tool before loading any site skill. If the "
            "generated tool returns success=true, do not load the skill; answer "
            "from the returned candidates/evidence. Only load the skill if the "
            "generated tool returns success=false.\n\n"
        )
    elif mode == "baseline_skill":
        policy = (
            "A/B mode: baseline_skill. Do not call dtc_site_search_tool, even if "
            "dtc_site_search_context says has_tool=true. After tiktok_sku_lookup "
            "and dtc_site_search_context, load the published site skill when "
            "available and follow it. This baseline intentionally measures the "
            "skill path without generated tool acceleration.\n\n"
        )
    else:
        raise ValueError(f"unknown mode: {mode}")
    return common + policy + "User request:\n" + case.prompt


def build_stateless_prompt(request: str, sku_id: str = "", site_url: str = "") -> str:
    if request.strip():
        return (
            "Run one stateless DTC same/similar-product search request. "
            "Do not rely on prior conversation context. Use tiktok_sku_lookup "
            "when a sku_id is present, then dtc_site_search_context. If a "
            "generated site tool is published, call dtc_site_search_tool first. "
            "If the generated tool returns success=true, do not load the site "
            "skill; answer from the returned candidates/evidence. Only load the "
            "site skill if the generated tool returns success=false. Record the completed exploration exactly once "
            "with dtc_site_search_record.\n\n"
            f"Request: {request.strip()}"
        )
    if not sku_id.strip() or not site_url.strip():
        raise ValueError("Provide either request or both sku_id and site_url")
    return (
        "Run one stateless DTC same/similar-product search request. "
        "Do not rely on prior conversation context. First call "
        "tiktok_sku_lookup for the sku_id, then call dtc_site_search_context "
        "for the site. If has_tool=true, call dtc_site_search_tool first and "
        "use its structured candidates. If it returns success=true, do not call "
        "skill_view; answer from the returned candidates/evidence. Only if the "
        "generated tool returns success=false, call skill_view for the learned "
        "site skill. Record the completed exploration exactly once with "
        "dtc_site_search_record. Return concise JSON-like findings with "
        "candidate URLs and evidence.\n\n"
        f"SKU ID: {sku_id.strip()}\nSite URL: {site_url.strip()}"
    )


def _toolsets() -> List[str]:
    return ["tiktok_sku", "dtc_site_search", "web", "browser", "vision", "skills", "terminal"]


def _session_metrics_from_agent_log(session_id: str) -> Dict[str, int]:
    if not session_id:
        return {}
    try:
        from hermes_constants import get_hermes_home

        log_path = get_hermes_home() / "logs" / "agent.log"
        text = log_path.read_text(encoding="utf-8", errors="ignore")[-2_000_000:]
    except Exception:
        return {}
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    api_calls = 0
    pattern = re.compile(
        r"\[" + re.escape(session_id) + r"\].*?API call #(\d+):.*? in=(\d+) out=(\d+) total=(\d+)"
    )
    for match in pattern.finditer(text):
        api_calls = max(api_calls, int(match.group(1)))
        input_tokens += int(match.group(2))
        output_tokens += int(match.group(3))
        total_tokens += int(match.group(4))
    return {
        "api_calls": api_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def run_agent_prompt(
    prompt: str,
    *,
    session_prefix: str,
    max_iterations: int = 30,
    model: str = "",
    agent_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    if agent_factory is None:
        from run_agent import AIAgent

        agent_factory = AIAgent
        try:
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider

            cfg = load_config()
            model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
            requested = str(model_cfg.get("provider") or "auto")
            configured_model = str(model or model_cfg.get("default") or "")
            runtime = resolve_runtime_provider(requested=requested, target_model=configured_model)
            runtime_model = runtime.get("model")
            if runtime_model and (not configured_model or configured_model == runtime.get("provider") or configured_model == runtime.get("name")):
                configured_model = str(runtime_model)
        except Exception:
            runtime = {}
            configured_model = model
    else:
        runtime = {}
        configured_model = model

    events: List[Dict[str, Any]] = []
    session_id = f"{session_prefix}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"
    started = time.time()

    def on_tool_start(tool_call_id: str, name: str, args: Dict[str, Any] | None = None, **_: Any) -> None:
        events.append({"type": "tool_start", "tool_call_id": tool_call_id, "name": name, "args": args or {}, "at": time.time()})

    def on_tool_complete(tool_call_id: str, name: str, args: Dict[str, Any] | None = None, result: Any = None, **_: Any) -> None:
        preview = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        events.append({
            "type": "tool_complete",
            "tool_call_id": tool_call_id,
            "name": name,
            "args": args or {},
            "preview": preview[:2000],
            "at": time.time(),
        })

    agent = agent_factory(
        model=configured_model,
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        acp_command=runtime.get("command"),
        acp_args=list(runtime.get("args") or []),
        credential_pool=runtime.get("credential_pool"),
        max_iterations=max_iterations,
        enabled_toolsets=_toolsets(),
        quiet_mode=True,
        platform="api_server",
        session_id=session_id,
        skip_memory=True,
        skip_context_files=True,
        tool_start_callback=on_tool_start,
        tool_complete_callback=on_tool_complete,
    )
    result = agent.run_conversation(prompt, conversation_history=[])
    actual_session_id = str(getattr(agent, "session_id", "") or session_id)
    messages = result.get("messages") or []
    final_response = result.get("final_response", "") or ""
    if not str(final_response or "").strip():
        for msg in reversed(messages):
            if msg.get("role") == "assistant" and str(msg.get("content") or "").strip():
                final_response = str(msg.get("content") or "")
                break
    input_tokens = int(result.get("input_tokens", 0) or result.get("prompt_tokens", 0) or 0)
    output_tokens = int(result.get("output_tokens", 0) or result.get("completion_tokens", 0) or 0)
    total_tokens = int(result.get("total_tokens", 0) or 0)
    if total_tokens <= 0:
        total_tokens = input_tokens + output_tokens
    if total_tokens <= 0:
        metrics = _session_metrics_from_agent_log(actual_session_id)
        input_tokens = int(metrics.get("input_tokens") or input_tokens or 0)
        output_tokens = int(metrics.get("output_tokens") or output_tokens or 0)
        total_tokens = int(metrics.get("total_tokens") or total_tokens or 0)
        if int(result.get("api_calls", 0) or 0) <= 0 and int(metrics.get("api_calls") or 0) > 0:
            result["api_calls"] = int(metrics.get("api_calls") or 0)
    return {
        "success": True,
        "session_id": actual_session_id,
        "elapsed_seconds": round(time.time() - started, 3),
        "final_response": final_response or "",
        "api_calls": result.get("api_calls", 0),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": result.get("estimated_cost_usd", 0),
        "events": events,
    }


def run_case(
    case: DtcSiteSearchAbCase,
    mode: str,
    max_iterations: int,
    model: str = "",
    agent_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    result = run_agent_prompt(
        build_mode_prompt(case, mode),
        session_prefix=f"dtc_ab_{mode}",
        max_iterations=max_iterations,
        model=model,
        agent_factory=agent_factory,
    )
    complete_events = [event for event in result.get("events") or [] if event.get("type") == "tool_complete"]
    final_response = result.get("final_response", "") or ""
    return {
        "case_index": case.index,
        "sku_id": case.sku_id,
        "product_id": case.product_id,
        "domain": case.domain,
        "mode": mode,
        "session_id": result.get("session_id"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "api_calls": result.get("api_calls", 0),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "total_tokens": result.get("total_tokens", 0),
        "estimated_cost_usd": result.get("estimated_cost_usd", 0),
        "tool_calls": [event.get("name") for event in complete_events],
        "tool_call_count": len(complete_events),
        "used_generated_tool": any(event.get("name") == "dtc_site_search_tool" for event in complete_events),
        "used_skill": any(event.get("name") == "skill_view" for event in complete_events),
        "expected": case.expected,
        "score": score_response(final_response, case.expected),
        "final_response": final_response,
        "events": result.get("events") or [],
    }


def summarize(results: List[Dict[str, Any]], min_token_reduction_rate: float = 0.05) -> Dict[str, Any]:
    by_mode: Dict[str, List[Dict[str, Any]]] = {}
    for result in results:
        by_mode.setdefault(result["mode"], []).append(result)

    mode_summary: Dict[str, Any] = {}
    for mode, rows in by_mode.items():
        judged = [row for row in rows if row["score"]["pass"] is not None]
        passed = [row for row in judged if row["score"]["pass"] is True]
        mode_summary[mode] = {
            "cases": len(rows),
            "judged_cases": len(judged),
            "pass_rate": (len(passed) / len(judged)) if judged else None,
            "avg_total_tokens": sum(row.get("total_tokens") or 0 for row in rows) / len(rows) if rows else 0,
            "avg_elapsed_seconds": sum(row.get("elapsed_seconds") or 0 for row in rows) / len(rows) if rows else 0,
            "avg_tool_calls": sum(row.get("tool_call_count") or 0 for row in rows) / len(rows) if rows else 0,
            "used_generated_tool_rate": sum(1 for row in rows if row.get("used_generated_tool")) / len(rows) if rows else 0,
            "used_skill_rate": sum(1 for row in rows if row.get("used_skill")) / len(rows) if rows else 0,
        }

    paired: List[Dict[str, Any]] = []
    by_case: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for result in results:
        by_case.setdefault(result["case_index"], {})[result["mode"]] = result
    for case_index, modes in by_case.items():
        cand = modes.get("candidate")
        base = modes.get("baseline_skill")
        if not cand or not base:
            continue
        base_tokens = base.get("total_tokens") or 0
        cand_tokens = cand.get("total_tokens") or 0
        token_reduction_rate = ((base_tokens - cand_tokens) / base_tokens) if base_tokens else 0.0
        paired.append({
            "case_index": case_index,
            "sku_id": cand["sku_id"],
            "candidate_pass": cand["score"]["pass"],
            "baseline_pass": base["score"]["pass"],
            "same_or_better_output": cand["score"]["pass"] is True and base["score"]["pass"] in {True, None},
            "token_delta": cand_tokens - base_tokens,
            "token_reduction_rate": token_reduction_rate,
            "elapsed_delta_seconds": (cand.get("elapsed_seconds") or 0) - (base.get("elapsed_seconds") or 0),
            "tool_call_delta": (cand.get("tool_call_count") or 0) - (base.get("tool_call_count") or 0),
        })
    publish_ready = bool(paired) and all(
        row["same_or_better_output"] and row["token_reduction_rate"] >= min_token_reduction_rate
        for row in paired
    )
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode_summary": mode_summary,
        "paired": paired,
        "paired_pass_count": sum(1 for row in paired if row["same_or_better_output"]),
        "paired_count": len(paired),
        "publish_ready": publish_ready,
        "publish_policy": {
            "requires_paired_cases": True,
            "requires_candidate_same_or_better_output": True,
            "min_token_reduction_rate": min_token_reduction_rate,
        },
    }


def run_ab_cases(
    cases: Iterable[DtcSiteSearchAbCase],
    modes: List[str],
    concurrency: int,
    max_iterations: int,
    model: str = "",
    agent_factory: Optional[Callable[..., Any]] = None,
    min_token_reduction_rate: float = 0.05,
) -> Dict[str, Any]:
    work = [(case, mode) for case in cases for mode in modes]
    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        future_map = {
            pool.submit(run_case, case, mode, max_iterations, model, agent_factory): (case, mode)
            for case, mode in work
        }
        for future in concurrent.futures.as_completed(future_map):
            case, mode = future_map[future]
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({
                    "case_index": case.index,
                    "sku_id": case.sku_id,
                    "domain": case.domain,
                    "mode": mode,
                    "error": str(exc),
                    "score": {"pass": False, "reason": "runner error"},
                })
    results.sort(key=lambda row: (row.get("case_index", -1), row.get("mode", "")))
    return {"summary": summarize(results, min_token_reduction_rate), "results": results}
