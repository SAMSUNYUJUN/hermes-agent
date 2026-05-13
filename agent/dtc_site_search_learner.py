"""Background learner for DTC site product-search explorations.

This module records one completed independent-site exploration at a time,
cleans it with an auxiliary LLM in a background thread, and promotes repeated
successful cleaned chains into a site-specific search skill.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)
_state_lock = threading.RLock()

ROOT_DIRNAME = "dtc_site_search"
SKILL_CATEGORY = "dtc-site-search"
INDEX_SKILL_NAME = "dtc-site-search-index"
DEFAULT_SUCCESS_THRESHOLD = 5
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = REPO_ROOT / "dtc_site_search_data"
DEFAULT_SKILL_ROOT = REPO_ROOT / "skills" / SKILL_CATEGORY
DEFAULT_INDEX_SKILL_ROOT = REPO_ROOT / "skills" / INDEX_SKILL_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def normalize_site_url(site_url: str) -> str:
    parsed = urlparse((site_url or "").strip())
    if not parsed.scheme:
        parsed = urlparse("https://" + (site_url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def site_key(site_url: str) -> str:
    normalized = normalize_site_url(site_url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    host = urlparse(normalized).netloc or "site"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:48] or "site"
    return f"{slug}-{digest}"


def skill_name_for_site(site_url: str) -> str:
    key = site_key(site_url)
    return f"dtc-site-{key}"[:64].rstrip("-")


def _site_dir(site_url: str) -> Path:
    root = os.getenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", "").strip()
    base = Path(root).expanduser() if root else DEFAULT_DATA_ROOT
    return base / site_key(site_url)


def _skill_dir(name: str) -> Path:
    root = os.getenv("HERMES_DTC_SITE_SEARCH_SKILL_DIR", "").strip()
    base = Path(root).expanduser() if root else DEFAULT_SKILL_ROOT
    return base / name


def _index_skill_dir() -> Path:
    root = os.getenv("HERMES_DTC_SITE_SEARCH_INDEX_SKILL_DIR", "").strip()
    return Path(root).expanduser() if root else DEFAULT_INDEX_SKILL_ROOT


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to read JSON from %s", path, exc_info=True)
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        Path(tmp_name).replace(path)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def get_success_threshold() -> int:
    """Return the configured successful-chain count needed per skill update."""
    import os

    raw = os.getenv("HERMES_DTC_SITE_SEARCH_SUCCESS_THRESHOLD", "").strip()
    if not raw:
        try:
            from hermes_cli.config import load_config

            raw = str(
                (load_config().get("dtc_site_search") or {}).get(
                    "success_threshold",
                    DEFAULT_SUCCESS_THRESHOLD,
                )
            )
        except Exception:
            raw = str(DEFAULT_SUCCESS_THRESHOLD)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_SUCCESS_THRESHOLD


def _extract_text(resp: Any) -> str:
    try:
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


def _compact_messages_for_cleanup(messages: List[Dict[str, Any]], max_chars: int = 42000) -> str:
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "")
        name = msg.get("tool_name") or msg.get("name") or ""
        label = f"{role}:{name}" if name else role
        content = msg.get("content", "")
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False, default=str)
            except Exception:
                content = str(content)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            try:
                content += "\nTOOL_CALLS: " + json.dumps(tool_calls, ensure_ascii=False, default=str)
            except Exception:
                content += "\nTOOL_CALLS: " + str(tool_calls)
        content = content.strip()
        if content:
            parts.append(f"## {label}\n{content}")
    text = "\n\n".join(parts)
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _load_session_context(session_id: str, tool_call_id: str = "") -> str:
    """Load original Hermes conversation from the built-in session DB.

    The current turn is usually flushed shortly after the tool returns, so the
    background cleaner retries briefly before falling back to the structured raw
    record only. Nothing is persisted outside Hermes' normal state.db.
    """
    if not session_id:
        return ""
    for attempt in range(8):
        try:
            from hermes_state import SessionDB

            messages = SessionDB().get_messages_as_conversation(session_id, include_ancestors=True)
            if messages:
                text = _compact_messages_for_cleanup(messages)
                if tool_call_id and tool_call_id in text:
                    return text
                if attempt >= 2:
                    return text
        except Exception:
            logger.debug("Failed to load DTC site-search session context", exc_info=True)
        time.sleep(1.5)
    return ""


def _aux_llm(system: str, user: str, task: str, max_tokens: int = 1200) -> str:
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client(task)
        if client is None or not model:
            return ""
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=max_tokens,
            timeout=90,
        )
        return _extract_text(resp).strip()
    except Exception:
        logger.info("DTC site-search auxiliary LLM call failed", exc_info=True)
        return ""


def get_site_skill_context(site_url: str) -> Dict[str, Any]:
    normalized = normalize_site_url(site_url)
    name = skill_name_for_site(normalized)
    state = _read_json(_site_dir(normalized) / "state.json", {})
    skill_path = ""
    direct_browse_protocol = (
        "DTC search protocol: after tiktok_sku_lookup, call this context tool. "
        "If has_skill=true, call skill_view(name=skill_view_name) and follow the "
        "loaded skill's Minimal Successful Path; it may tell you to navigate "
        "directly to a redirect target, catalog host, category page, or product "
        "listing URL instead of the user-provided URL. Do not repeat anything "
        "listed under Do Not Do. If has_skill=false, use browser_navigate on the "
        "user-provided DTC URL and explore directly before any Google/web_search."
    )
    generated_skill_md = _skill_dir(name) / "SKILL.md"
    if generated_skill_md.exists():
        skill_path = str(generated_skill_md)
        return {
            "success": True,
            "site_url": normalized,
            "site_key": site_key(normalized),
            "skill_name": name,
            "skill_view_name": name,
            "skill_path": skill_path,
            "success_count": int(state.get("success_count", 0) or 0),
            "has_skill": True,
            "direct_browse_protocol": direct_browse_protocol,
            "load_instruction": (
                f"Call skill_view(name='{name}') before browsing this site. "
                "Follow the loaded site's Minimal Successful Path even when it "
                "starts on a redirect target or catalog URL instead of the original "
                "user-provided URL. Skip every anti-route in Do Not Do."
            ),
        }
    return {
        "success": True,
        "site_url": normalized,
        "site_key": site_key(normalized),
        "skill_name": name,
        "skill_view_name": name,
        "skill_path": skill_path,
        "success_count": int(state.get("success_count", 0) or 0),
        "has_skill": False,
        "direct_browse_protocol": direct_browse_protocol,
        "load_instruction": "",
    }


def record_exploration(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_site_url(str(payload.get("site_url") or ""))
    if not urlparse(normalized).netloc:
        return {"success": False, "error": "site_url is required"}
    sku_id = str(payload.get("sku_id") or "").strip()
    if not sku_id:
        return {"success": False, "error": "sku_id is required"}

    now = _utc_now()
    record_id = f"{now}-{hashlib.sha1(json.dumps(payload, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:8]}"
    root = _site_dir(normalized)
    raw_path = root / "raw" / f"{record_id}.json"
    raw = {
        "record_id": record_id,
        "created_at": now,
        "site_url": normalized,
        "site_key": site_key(normalized),
        "sku_id": sku_id,
        "success": bool(payload.get("success")),
        "session_id": str(payload.get("session_id") or ""),
        "tool_call_id": str(payload.get("tool_call_id") or ""),
        "payload": payload,
    }
    _write_json(raw_path, raw)

    thread = threading.Thread(
        target=_clean_and_promote,
        args=(normalized, record_id, raw),
        name=f"dtc-site-search-{record_id}",
        daemon=True,
    )
    thread.start()

    return {
        "success": True,
        "message": "DTC site exploration recorded; background cleanup started.",
        "site_url": normalized,
        "site_key": site_key(normalized),
        "record_id": record_id,
        "raw_path": str(raw_path),
        "skill_name": skill_name_for_site(normalized),
    }


def _clean_and_promote(site_url: str, record_id: str, raw: Dict[str, Any]) -> None:
    root = _site_dir(site_url)
    cleaned_dir = root / "cleaned"
    state_path = root / "state.json"
    cleaned_path = cleaned_dir / f"{record_id}.md"

    payload = raw.get("payload", {})
    session_context = _load_session_context(
        str(raw.get("session_id") or payload.get("session_id") or ""),
        str(raw.get("tool_call_id") or payload.get("tool_call_id") or ""),
    )
    system = (
        "You clean DTC independent-site product-search explorations. "
        "Extract the shortest coherent successful search chain when one exists. "
        "Separate useful steps from wasted exploration. Preserve concrete page "
        "URLs, redirects, selectors, search terms, navigation choices, product "
        "candidates, and pitfalls. Do not decide same-item match."
    )
    user = (
        "Clean this exploration into Markdown with sections: Result, "
        "Minimal successful chain, Wasted or wrong paths, Candidate products, "
        "Pitfalls, Reusable notes.\n\n"
        "The Minimal successful chain must be the fewest supported actions needed "
        "to reach useful product candidates on this website. If the original run "
        "first tried an old URL, an ineffective search box, extra snapshots, or "
        "wrong buttons before discovering the real route, do not put those steps "
        "in the minimal chain. Put them under Wasted or wrong paths with explicit "
        "`do not ...` guidance.\n\n"
        "Use the Hermes conversation transcript as the primary source when it "
        "is available. Use the structured raw record as an index/summary and "
        "for metadata. Do not invent steps that are not supported by either "
        "source.\n\n"
        "## Structured raw record\n"
        + json.dumps(raw, ensure_ascii=False, indent=2)[:18000]
        + "\n\n## Hermes conversation from state.db\n"
        + (session_context or "Unavailable; clean from structured raw record only.")
    )
    cleaned = _aux_llm(system, user, "dtc_site_search_cleanup", max_tokens=1800)
    if not cleaned:
        cleaned = _fallback_cleaned_markdown(raw)
    cleaned_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned_path.write_text(cleaned.strip() + "\n", encoding="utf-8")

    with _state_lock:
        state = _read_json(state_path, {})
        records: List[Dict[str, Any]] = list(state.get("records") or [])
        records.append({
            "record_id": record_id,
            "sku_id": raw.get("sku_id"),
            "success": bool(raw.get("success")),
            "cleaned_path": str(cleaned_path),
            "created_at": raw.get("created_at"),
        })
        success_count = int(state.get("success_count", 0) or 0)
        if raw.get("success"):
            success_count += 1
        threshold = get_success_threshold()
        last_update_count = int(state.get("last_skill_update_success_count", 0) or 0)
        state.update({
            "site_url": site_url,
            "site_key": site_key(site_url),
            "skill_name": skill_name_for_site(site_url),
            "success_count": success_count,
            "success_threshold": threshold,
            "records": records[-100:],
            "updated_at": _utc_now(),
        })
        _write_json(state_path, state)

    if raw.get("success") and success_count >= threshold and success_count - last_update_count >= threshold:
        _promote_site_skill(site_url, success_count, threshold)


def _fallback_cleaned_markdown(raw: Dict[str, Any]) -> str:
    payload = raw.get("payload", {})
    return "\n".join([
        f"# DTC Site Search Exploration {raw.get('record_id')}",
        "",
        "## Result",
        f"- Site: {raw.get('site_url')}",
        f"- SKU: {raw.get('sku_id')}",
        f"- Success: {bool(raw.get('success'))}",
        "",
        "## Successful chain",
        str(payload.get("successful_steps") or payload.get("exploration_summary") or ""),
        "",
        "## Candidate products",
        json.dumps(payload.get("candidate_products") or payload.get("product_urls") or [], ensure_ascii=False, indent=2),
        "",
        "## Pitfalls",
        json.dumps(payload.get("pitfalls") or [], ensure_ascii=False, indent=2),
    ])


def _promote_site_skill(site_url: str, success_count: int, threshold: int) -> None:
    root = _site_dir(site_url)
    state = _read_json(root / "state.json", {})
    records = [r for r in state.get("records", []) if r.get("success")]
    cleaned_chunks: List[str] = []
    for rec in records[-threshold:]:
        path = Path(str(rec.get("cleaned_path") or ""))
        try:
            cleaned_chunks.append(path.read_text(encoding="utf-8")[:12000])
        except Exception:
            continue
    if len(cleaned_chunks) < threshold:
        return

    name = skill_name_for_site(site_url)
    existing = ""
    existing_skill_md = _skill_dir(name) / "SKILL.md"
    if existing_skill_md.exists():
        try:
            existing = existing_skill_md.read_text(encoding="utf-8")
        except Exception:
            logger.debug("Failed to read existing DTC site skill %s", name, exc_info=True)
    system = (
        "You maintain Hermes skills for searching one DTC independent site. "
        "Use only cleaned exploration chains, not raw transcripts. "
        "The skill must teach the minimal successful path for this website, not "
        "a narrative of everything tried. Optimize for saving tokens and tool "
        "calls on the next run. If the winning path uses a redirect target or "
        "related catalog domain, tell the agent to go directly to the durable "
        "target URL; do not add a ceremonial first visit to the old URL unless "
        "that visit is technically required for cookies, geo routing, or auth. "
        "Explicitly warn not to keep searching the old/redirecting surface. "
        "If a search box, menu, popup, button, snapshot, "
        "or page area was wasteful, name it in a negative instruction so the "
        "agent skips it. Focus on durable website behavior: whether search is "
        "useful, exact entry URL or redirected catalog domain, durable all-products "
        "or collection paths, tiny-catalog enumeration, result URL patterns, and "
        "the few verification signals needed after reaching product pages. Do not "
        "include specific SKU IDs, one-off marketplace titles, one-off search "
        "queries, candidate product details, price-comparison logic, or final "
        "same-item judgment logic."
    )
    user = (
        f"Site URL: {site_url}\n"
        f"Skill name: {name}\n"
        f"Successful cleaned chain count: {success_count}\n\n"
        f"Existing SKILL.md, if any:\n{existing[:16000]}\n\n"
        "Latest cleaned chains. Extract only reusable website-search lessons; "
        "do not copy the concrete examples into the skill. Collapse them into "
        "the shortest repeatable route and explicit anti-routes:\n\n"
        + "\n\n---\n\n".join(cleaned_chunks)
        + (
            "\n\nReturn a complete SKILL.md with valid YAML frontmatter. "
            "Use exactly these sections: When To Use, Minimal Successful Path, "
            "Do Not Do, Product Discovery Shortcuts, Verification Hints. "
            "`Minimal Successful Path` must be an ordered checklist of the fewest "
            "browser/tool actions to reach candidate product pages. `Do Not Do` "
            "must list known bad clicks, ineffective search surfaces, avoidable "
            "snapshots, old domains after redirects, popups, or broad exploration "
            "that wasted steps. Keep it concise, imperative, and website-focused."
        )
    )
    fallback_content = _fallback_skill(site_url, name, cleaned_chunks)
    content = _aux_llm(system, user, "dtc_site_search_skill", max_tokens=2600)
    if not content or not content.lstrip().startswith("---"):
        content = fallback_content

    wrote = _write_skill_background(name, content, bool(existing))
    if not wrote and content != fallback_content:
        wrote = _write_skill_background(name, fallback_content, bool(existing))
    if not wrote:
        return
    try:
        refresh_site_skill_index()
    except Exception:
        logger.debug("Failed to refresh DTC site-search index skill", exc_info=True)

    with _state_lock:
        state = _read_json(root / "state.json", {})
        state["last_skill_update_success_count"] = success_count
        state["last_skill_update_at"] = _utc_now()
        state["success_threshold"] = threshold
        _write_json(root / "state.json", state)


def _fallback_skill(site_url: str, name: str, cleaned_chunks: List[str]) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Search this DTC independent site for candidate products matching a TikTok SKU.\n"
        "version: 1.0.0\n"
        "metadata:\n"
        "  hermes:\n"
        "    category: dtc-site-search\n"
        "    tags: [dtc, product-search, tiktok-sku]\n"
        "---\n\n"
        f"# {name}\n\n"
        f"Use this skill when searching `{site_url}` for products that may match a TikTok SKU. "
        "Load TikTok SKU title, description, and images first. Use this skill "
        "only for website-specific minimal search routing; do not use it to "
        "decide the final same-item match.\n\n"
        "## Minimal Successful Path\n\n"
        "- Follow the shortest successful route preserved in the cleaned chains.\n"
        "- Prefer direct product catalog URLs, redirect targets, collection pages, "
        "all-products pages, or tiny-catalog enumeration over broad exploration.\n"
        "- Do not spend tool calls on preliminary snapshots once a durable direct "
        "entry path is known.\n\n"
        "## Do Not Do\n\n"
        "- Do not repeat failed search boxes, wrong menu clicks, old redirecting "
        "surfaces, popup interactions, or broad browsing steps listed in the "
        "cleaned chains.\n"
        "- Do not preserve one-off SKU search queries as future instructions unless "
        "they reveal durable site behavior.\n\n"
        "## Product Discovery Shortcuts\n\n"
        "- Prefer durable URL patterns, related catalog domains, all-products pages, "
        "and product listing pages that led to candidates.\n\n"
        "## Verification Hints\n\n"
        "- After reaching a candidate product page, preserve product IDs, canonical "
        "URLs, title, size, and image evidence for the downstream matcher.\n"
        "- Do not include price comparison or final same-item judgment in this skill.\n"
    )


def refresh_site_skill_index() -> str:
    """Write the index skill that tells agents which DTC site skills exist."""
    data_root = Path(os.getenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", "").strip() or DEFAULT_DATA_ROOT)
    entries: List[Dict[str, str]] = []
    if data_root.exists():
        for state_path in sorted(data_root.glob("*/state.json")):
            state = _read_json(state_path, {})
            site_url = str(state.get("site_url") or "").strip()
            skill_name = str(state.get("skill_name") or "").strip()
            if not site_url or not skill_name:
                continue
            skill_md = _skill_dir(skill_name) / "SKILL.md"
            if skill_md.exists():
                entries.append({
                    "site_url": site_url,
                    "site_key": str(state.get("site_key") or state_path.parent.name),
                    "skill_name": skill_name,
                    "skill_path": str(skill_md),
                    "success_count": str(state.get("success_count", 0)),
                })

    lines = [
        "---",
        f"name: {INDEX_SKILL_NAME}",
        "description: Index of available DTC independent-site search strategy skills.",
        "version: 1.0.0",
        "metadata:",
        "  hermes:",
        "    category: dtc-site-search",
        "    tags: [dtc, product-search, skill-index]",
        "---",
        "",
        "# DTC Site Search Skill Index",
        "",
        "## When To Use",
        "",
        "Load this skill when the user provides a TikTok SKU and an independent-site URL. It tells you which site-specific DTC search strategy skills are available.",
        "",
        "## Search Order",
        "",
        "1. Call `tiktok_sku_lookup` for the SKU evidence.",
        "2. Call `dtc_site_search_context(site_url)` and, if it returns `has_skill=true`, immediately call `skill_view(name=skill_view_name)` before browsing.",
        "3. If a site skill is loaded, follow its `Minimal Successful Path` exactly. That path may start at a redirect target, catalog host, category URL, or product listing URL instead of the user-provided URL.",
        "4. Do not repeat any route, click, search box, snapshot pattern, or broad exploration listed in the loaded skill's `Do Not Do` section.",
        "5. Only when no site skill exists, use `browser_navigate` on the user's DTC URL, then inspect with `browser_snapshot`, `browser_click`, `browser_type`, and `browser_press`.",
        "6. Only use web search after direct browser exploration fails or reveals that the product catalog lives on a related domain.",
        "",
        "## Available Site Skills",
        "",
    ]
    if entries:
        for entry in entries:
            lines.append(
                f"- `{entry['site_url']}` -> `{entry['skill_name']}` "
                f"(success_count={entry['success_count']}, path=`{entry['skill_path']}`)"
            )
    else:
        lines.append("- No site-specific DTC search skills have been generated yet.")

    lines.extend([
        "",
        "## Visual Matching Reminder",
        "",
        "When the user asks whether the TikTok SKU and a site candidate are the same item, use vision to compare product images if both sides have images. If either side has no image, skip visual comparison.",
    ])

    skill_md = _index_skill_dir() / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    tmp = skill_md.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(skill_md)
    return str(skill_md)


def _write_skill_background(name: str, content: str, exists: bool) -> bool:
    try:
        skill_md = _skill_dir(name) / "SKILL.md"
        skill_md.parent.mkdir(parents=True, exist_ok=True)
        tmp = skill_md.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(skill_md)
        return True
    except Exception:
        logger.info("Failed to write DTC site-search skill %s", name, exc_info=True)
        return False
