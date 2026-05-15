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
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)
_state_lock = threading.RLock()

ROOT_DIRNAME = "dtc_site_search"
SKILL_CATEGORY = "dtc-site-search"
INDEX_SKILL_NAME = "dtc-site-search-index"
DEFAULT_SUCCESS_THRESHOLD = 2
DEFAULT_SKILL_UPDATE_WINDOW = 1
DEFAULT_TOOL_GENERATION_STABLE_UPDATES = 2
DEFAULT_TOOL_FAILURE_DISABLE_THRESHOLD = 3
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = REPO_ROOT / "dtc_site_search_data"
DEFAULT_SKILL_ROOT = REPO_ROOT / "skills" / SKILL_CATEGORY
DEFAULT_INDEX_SKILL_ROOT = REPO_ROOT / "skills" / INDEX_SKILL_NAME
ACTIVE_ROUTE_SKILL_ONLY = "skill_only"
ACTIVE_ROUTE_TOOL_FIRST = "tool_first"
TOOL_STATUS_NONE = "none"
TOOL_STATUS_BUILDING = "building"
TOOL_STATUS_TESTING = "testing"
TOOL_STATUS_ACTIVE = "active"
TOOL_STATUS_DISABLED = "disabled"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_utc(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_tool_generation_stale_seconds() -> int:
    value = os.getenv("HERMES_DTC_SITE_SEARCH_TOOL_GENERATION_STALE_SECONDS", "").strip()
    if value:
        try:
            return max(60, int(value))
        except ValueError:
            pass
    return 300


def _is_tool_generation_stale(state: Dict[str, Any]) -> bool:
    if str(state.get("tool_status") or "") not in {TOOL_STATUS_BUILDING, TOOL_STATUS_TESTING}:
        return False
    tool_info = state.get("generated_tool") or {}
    started = str(state.get("last_tool_generation_started_at") or tool_info.get("started_at") or "")
    started_at = _parse_utc(started)
    if not started_at:
        return True
    return (datetime.now(timezone.utc) - started_at).total_seconds() >= get_tool_generation_stale_seconds()


def normalize_site_url(site_url: str) -> str:
    parsed = urlparse((site_url or "").strip())
    if not parsed.scheme:
        parsed = urlparse("https://" + (site_url or "").strip())
    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Treat `www.` as the same host, but preserve an explicit path so a user
    # can train a different entry point such as hsn.com/product/.
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def _site_key_from_normalized_exact(normalized: str) -> str:
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    host = urlparse(normalized).netloc or "site"
    slug = re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")[:48] or "site"
    return f"{slug}-{digest}"


def site_key(site_url: str) -> str:
    normalized = normalize_site_url(site_url)
    return _site_key_from_normalized_exact(normalized)


def skill_name_for_site(site_url: str) -> str:
    key = site_key(site_url)
    return f"dtc-site-{key}"[:64].rstrip("-")


def _site_dir(site_url: str) -> Path:
    return _data_root() / site_key(site_url)


def _data_root() -> Path:
    root = os.getenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", "").strip()
    return Path(root).expanduser() if root else DEFAULT_DATA_ROOT


def _legacy_site_dirs(site_url: str) -> List[Path]:
    """Return pre-canonicalization dirs for the same logical site.

    Older learner versions keyed by exact URL host, so `www.` and bare hosts
    could create separate state dirs. Keep those dirs in place for raw/cleaned
    file references, but merge records for the exact same path after `www.`
    normalization.
    """
    normalized = normalize_site_url(site_url)
    canonical_dir = _site_dir(normalized)
    base = _data_root()
    dirs: List[Path] = []
    if not base.exists():
        return dirs

    for state_path in sorted(base.glob("*/state.json")):
        candidate_dir = state_path.parent
        if candidate_dir == canonical_dir:
            continue
        state = _read_json(state_path, {})
        legacy_url = str(state.get("site_url") or "").strip()
        if legacy_url and normalize_site_url(legacy_url) == normalized:
            dirs.append(candidate_dir)
    return dirs


def _merge_legacy_site_state(site_url: str) -> Dict[str, Any]:
    """Merge pre-canonicalization records into the canonical site state.

    Existing cleaned/raw files stay where they are, but every state record for
    the same canonical URL contributes to the one canonical success counter and
    one site skill.
    """
    normalized = normalize_site_url(site_url)
    canonical_root = _site_dir(normalized)
    state_path = canonical_root / "state.json"
    state = _read_json(state_path, {})
    legacy_dirs = _legacy_site_dirs(normalized)
    if not legacy_dirs:
        return state

    merged_records: Dict[str, Dict[str, Any]] = {}
    for source_state in [_read_json(d / "state.json", {}) for d in legacy_dirs] + [state]:
        for rec in list(source_state.get("records") or []):
            record_id = str(rec.get("record_id") or rec.get("cleaned_path") or "")
            if record_id:
                merged_records[record_id] = rec
    if not merged_records:
        return state

    records = sorted(
        merged_records.values(),
        key=lambda rec: str(rec.get("created_at") or rec.get("record_id") or ""),
    )
    success_count = sum(1 for rec in records if rec.get("success"))
    state.update({
        "site_url": normalized,
        "site_key": site_key(normalized),
        "skill_name": skill_name_for_site(normalized),
        "success_count": success_count,
        "records": records[-100:],
        "merged_legacy_site_keys": [d.name for d in legacy_dirs],
        # Back-compat for older debug/status readers.
        "merged_legacy_www_site_key": next((d.name for d in legacy_dirs if d.name.startswith("www-")), ""),
        "updated_at": _utc_now(),
    })
    _write_json(state_path, state)
    return state


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


def _canonical_tool_status(tool_info: Dict[str, Any]) -> str:
    if not tool_info:
        return TOOL_STATUS_NONE
    status = str(tool_info.get("status") or "").strip()
    if status in {TOOL_STATUS_ACTIVE, TOOL_STATUS_DISABLED, TOOL_STATUS_BUILDING, TOOL_STATUS_TESTING, TOOL_STATUS_NONE}:
        return status
    if status == "enabled" or tool_info.get("enabled"):
        return TOOL_STATUS_ACTIVE
    if status in {"generating", "repairing"}:
        return TOOL_STATUS_BUILDING
    return status or TOOL_STATUS_NONE


def _ensure_domain_state_shape(state: Dict[str, Any], site_url: str) -> Dict[str, Any]:
    """Backfill per-domain state-machine fields without changing existing N/X/Y flow."""
    normalized = normalize_site_url(site_url or str(state.get("site_url") or ""))
    tool_info = dict(state.get("generated_tool") or {})
    tool_status = str(state.get("tool_status") or _canonical_tool_status(tool_info))
    active_tool_version = str(state.get("active_tool_version") or "")
    if tool_status == TOOL_STATUS_ACTIVE and not active_tool_version:
        active_tool_version = str(tool_info.get("version") or "")
    if tool_status != TOOL_STATUS_ACTIVE:
        active_tool_version = ""
    active_route = str(state.get("active_route") or "")
    if not active_route:
        active_route = ACTIVE_ROUTE_TOOL_FIRST if tool_status == TOOL_STATUS_ACTIVE else ACTIVE_ROUTE_SKILL_ONLY
    success_count = int(state.get("success_count", 0) or 0)
    last_update_count = int(state.get("last_skill_update_success_count", 0) or 0)
    stable_count = int(state.get("stable_skill_update_count", state.get("y_no_change_count", 0)) or 0)
    skill_name = str(state.get("skill_name") or skill_name_for_site(normalized))
    has_published_skill = (_skill_dir(skill_name) / "SKILL.md").exists()
    state.setdefault("domain", urlparse(normalized).netloc or normalized)
    state.setdefault("site_url", normalized)
    state["n_success_count"] = 0 if has_published_skill else max(0, success_count - last_update_count)
    state.setdefault("x_success_count", max(0, success_count - last_update_count))
    state.setdefault("y_no_change_count", stable_count)
    state.setdefault("z_tool_fail_count", int(tool_info.get("z_tool_fail_count", 0) or 0))
    state.setdefault("skill_status", "active" if has_published_skill else "none")
    state.setdefault("active_skill_version", str(state.get("active_skill_version") or "current"))
    state["tool_status"] = tool_status
    state["active_tool_version"] = active_tool_version
    state["active_route"] = active_route
    state.setdefault("state_version", int(state.get("state_version", 0) or 0))
    return state


def _bump_state_version(state: Dict[str, Any]) -> None:
    state["state_version"] = int(state.get("state_version", 0) or 0) + 1
    state["updated_at"] = _utc_now()


def _classify_tool_failure(result: Dict[str, Any]) -> str:
    error = str(result.get("error") or "").lower()
    if result.get("failure_type"):
        return str(result.get("failure_type"))
    if "timeout" in error or "timed out" in error:
        return "timeout"
    if "non-json" in error or "json" in error or "schema" in error:
        return "schema_error"
    if "network" in error or "fetch" in error or "econn" in error:
        return "network_error"
    if "no candidates" in error or "empty" in error:
        return "empty_result"
    if "exited" in error or "node executable" in error:
        return "execution_error"
    return "unknown"


def _tool_failure_event_id(
    site_url: str,
    tool_version: str,
    query: str,
    result: Dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
) -> str:
    if tool_call_id:
        return f"tool_call:{tool_call_id}"
    if session_id:
        return f"session:{session_id}:{tool_version}:{hashlib.sha1(query.encode('utf-8')).hexdigest()[:12]}"
    payload = {
        "site_url": normalize_site_url(site_url),
        "tool_version": tool_version,
        "query": query,
        "error": str(result.get("error") or ""),
        "failure_type": _classify_tool_failure(result),
    }
    return "hash:" + hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def get_success_threshold() -> int:
    """Return the successful-chain count needed before initial skill creation."""
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


def get_skill_update_window() -> int:
    """Return X: successful fallback records needed after a skill exists."""
    raw = os.getenv("HERMES_DTC_SITE_SEARCH_SKILL_UPDATE_WINDOW", "").strip()
    if not raw:
        try:
            from hermes_cli.config import load_config

            raw = str(
                (load_config().get("dtc_site_search") or {}).get(
                    "skill_update_window",
                    DEFAULT_SKILL_UPDATE_WINDOW,
                )
            )
        except Exception:
            raw = str(DEFAULT_SKILL_UPDATE_WINDOW)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_SKILL_UPDATE_WINDOW


def get_tool_generation_stable_updates() -> int:
    """Return Y: unchanged skill-update passes before generating a site tool."""
    raw = os.getenv("HERMES_DTC_SITE_SEARCH_TOOL_GENERATION_STABLE_UPDATES", "").strip()
    if not raw:
        try:
            from hermes_cli.config import load_config

            raw = str(
                (load_config().get("dtc_site_search") or {}).get(
                    "tool_generation_stable_updates",
                    DEFAULT_TOOL_GENERATION_STABLE_UPDATES,
                )
            )
        except Exception:
            raw = str(DEFAULT_TOOL_GENERATION_STABLE_UPDATES)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_TOOL_GENERATION_STABLE_UPDATES


def get_tool_failure_disable_threshold() -> int:
    """Return Z: active generated-tool failures before disabling that tool."""
    raw = os.getenv("HERMES_DTC_SITE_SEARCH_TOOL_FAILURE_DISABLE_THRESHOLD", "").strip()
    if not raw:
        try:
            from hermes_cli.config import load_config

            raw = str(
                (load_config().get("dtc_site_search") or {}).get(
                    "tool_failure_disable_threshold",
                    DEFAULT_TOOL_FAILURE_DISABLE_THRESHOLD,
                )
            )
        except Exception:
            raw = str(DEFAULT_TOOL_FAILURE_DISABLE_THRESHOLD)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_TOOL_FAILURE_DISABLE_THRESHOLD


def _skill_creation_ab_enabled() -> bool:
    raw = os.getenv("HERMES_DTC_SITE_SEARCH_SKILL_CREATION_AB_ENABLED", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def _generated_tool_path(site_url: str) -> Path:
    return _site_dir(site_url) / "generated_tool" / "site_search_tool.mjs"


def _generated_tool_versions_dir(site_url: str) -> Path:
    return _site_dir(site_url) / "generated_tool" / "versions"


def _new_version_id(prefix: str = "v") -> str:
    digest = hashlib.sha256(f"{time.time_ns()}:{threading.get_ident()}".encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{int(time.time() * 1000)}-{digest}"


def _generated_tool_index_path() -> Path:
    root = os.getenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", "").strip()
    base = Path(root).expanduser() if root else DEFAULT_DATA_ROOT
    return base / "generated_tools_index.json"


def _normalize_skill_content(content: str) -> str:
    return "\n".join(line.rstrip() for line in (content or "").strip().splitlines()).strip()


def _load_generated_tool_index() -> Dict[str, Any]:
    return _read_json(_generated_tool_index_path(), {"sites": {}})


def refresh_generated_tool_index() -> Dict[str, Any]:
    """Build the progressive registry of enabled generated site tools.

    This is intentionally a data index, not a set of eagerly loaded tool
    schemas. The main agent only sees the generic dispatcher tool. A site's
    generated-tool instructions are returned only after
    dtc_site_search_context(site_url) finds that specific site in this index.
    """
    data_root = Path(os.getenv("HERMES_DTC_SITE_SEARCH_DATA_DIR", "").strip() or DEFAULT_DATA_ROOT)
    sites: Dict[str, Dict[str, Any]] = {}
    if data_root.exists():
        for state_path in sorted(data_root.glob("*/state.json")):
            state = _read_json(state_path, {})
            site_url = str(state.get("site_url") or "").strip()
            if not site_url:
                continue
            normalized = normalize_site_url(site_url)
            tool_info = state.get("generated_tool") or {}
            state = _ensure_domain_state_shape(state, normalized)
            script_path = Path(str(tool_info.get("path") or "")) if tool_info.get("path") else _generated_tool_path(normalized)
            if state.get("tool_status") != TOOL_STATUS_ACTIVE or state.get("active_route") != ACTIVE_ROUTE_TOOL_FIRST:
                continue
            if not tool_info.get("enabled") or not script_path.exists():
                continue
            sites[site_key(normalized)] = {
                "site_url": normalized,
                "site_key": site_key(normalized),
                "skill_name": str(state.get("skill_name") or skill_name_for_site(normalized)),
                "tool_name": "dtc_site_search_tool",
                "tool_path": str(script_path),
                "tool_version": str(tool_info.get("version") or ""),
                "generated_at": str(tool_info.get("generated_at") or ""),
                "description": str(tool_info.get("description") or ""),
            }
    index = {"updated_at": _utc_now(), "sites": sites}
    _write_json(_generated_tool_index_path(), index)
    return index


def get_generated_tool_entry(site_url: str) -> Dict[str, Any]:
    normalized = normalize_site_url(site_url)
    index = _load_generated_tool_index()
    entry = (index.get("sites") or {}).get(site_key(normalized))
    script_path = Path(str((entry or {}).get("tool_path") or ""))
    if entry and script_path.exists():
        return dict(entry)

    # The index may be stale after a background generation or manual state move.
    state = _read_json(_site_dir(normalized) / "state.json", {})
    state = _ensure_domain_state_shape(state, normalized)
    tool_info = state.get("generated_tool") or {}
    state_script_path = Path(str(tool_info.get("path") or "")) if tool_info.get("path") else _generated_tool_path(normalized)
    if (
        state.get("tool_status") == TOOL_STATUS_ACTIVE
        and state.get("active_route") == ACTIVE_ROUTE_TOOL_FIRST
        and tool_info.get("enabled")
        and state_script_path.exists()
    ):
        index = refresh_generated_tool_index()
        return dict((index.get("sites") or {}).get(site_key(normalized)) or {})
    return {}


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


def _aux_llm_call_once(
    call_llm,
    extract_content_or_reasoning,
    system: str,
    user: str,
    task: str,
    max_tokens: int,
    timeout: float,
) -> str:
    result_queue: queue.Queue = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            resp = call_llm(
                task=task,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            result_queue.put(("ok", extract_content_or_reasoning(resp).strip()))
        except Exception as exc:
            result_queue.put(("error", exc))

    thread = threading.Thread(target=_run, name=f"dtc-site-aux-{task}", daemon=True)
    thread.start()
    try:
        status, value = result_queue.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError(f"auxiliary LLM task={task} exceeded {timeout:.0f}s timeout") from exc
    if status == "error":
        raise value
    return str(value or "")


def _aux_llm(system: str, user: str, task: str, max_tokens: int = 1200) -> str:
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    try:
        attempts = max(1, int(os.getenv("HERMES_DTC_SITE_SEARCH_AUX_ATTEMPTS", "4")))
    except (TypeError, ValueError):
        attempts = 4
    try:
        timeout = max(30.0, float(os.getenv("HERMES_DTC_SITE_SEARCH_AUX_TIMEOUT", "300")))
    except (TypeError, ValueError):
        timeout = 300.0

    deadline = time.monotonic() + timeout
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_exc = TimeoutError(f"auxiliary LLM task={task} exceeded overall {timeout:.0f}s timeout")
            break
        try:
            text = _aux_llm_call_once(
                call_llm,
                extract_content_or_reasoning,
                system,
                user,
                task,
                max_tokens,
                max(1.0, remaining),
            )
            if text:
                return text
            logger.info(
                "DTC site-search auxiliary LLM returned empty text "
                "(task=%s attempt=%d/%d)",
                task,
                attempt + 1,
                attempts,
            )
        except Exception as exc:
            last_exc = exc
            logger.info(
                "DTC site-search auxiliary LLM call failed "
                "(task=%s attempt=%d/%d); waiting before retry",
                task,
                attempt + 1,
                attempts,
                exc_info=True,
            )
        if attempt < attempts - 1:
            sleep_for = min(120, 10 * (2 ** attempt), max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                time.sleep(sleep_for)

    if last_exc is not None:
        logger.warning(
            "DTC site-search auxiliary LLM exhausted retries for task=%s; "
            "falling back to deterministic synthesis",
            task,
        )
    return ""


def get_site_skill_context(site_url: str) -> Dict[str, Any]:
    normalized = normalize_site_url(site_url)
    name = skill_name_for_site(normalized)
    state = _merge_legacy_site_state(normalized)
    state = _ensure_domain_state_shape(state, normalized)
    generated_skill_md = _skill_dir(name) / "SKILL.md"
    # This is a hot read path used by online searches. Skill/tool lifecycle
    # transitions are driven by background record cleanup so concurrent search
    # requests keep using the currently published version and do not block on
    # synthesis, A/B tests, or rollout work.
    skill_path = ""
    tool_entry = get_generated_tool_entry(normalized)
    has_tool = bool(tool_entry)
    direct_browse_protocol = (
        "DTC search protocol: after tiktok_sku_lookup, call this context tool. "
        "If has_tool=true, call dtc_site_search_tool first and use its structured "
        "candidate output. If that tool returns success=true, do not load the "
        "site skill; answer from its candidates/evidence, including no-match "
        "answers. Only if that tool returns success=false, fall back to the learned skill when has_skill=true. If has_skill=true, "
        "call skill_view(name=skill_view_name) and follow the loaded skill's "
        "Minimal Successful Path; it may tell you to navigate directly to a "
        "redirect target, catalog host, category page, or product listing URL "
        "instead of the user-provided URL. Do not repeat anything listed under "
        "Do Not Do. If neither tool nor skill is available, use browser_navigate "
        "on the user-provided DTC URL and explore directly before any Google/web_search."
    )
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
            "active_route": state.get("active_route", ACTIVE_ROUTE_SKILL_ONLY),
            "tool_status": state.get("tool_status", TOOL_STATUS_NONE),
            "active_tool_version": state.get("active_tool_version", ""),
            "z_tool_fail_count": int(state.get("z_tool_fail_count", 0) or 0),
            "tool_failure_disable_threshold": get_tool_failure_disable_threshold(),
            "has_tool": has_tool,
            "tool_name": tool_entry.get("tool_name", "") if has_tool else "",
            "tool_path": tool_entry.get("tool_path", "") if has_tool else "",
            "tool_intro": (
                tool_entry.get("description")
                or f"Use dtc_site_search_tool for {normalized} before loading the skill."
            ) if has_tool else "",
            "has_skill": True,
            "direct_browse_protocol": direct_browse_protocol,
            "load_instruction": (
                (
                    "Call dtc_site_search_tool(site_url=..., query=...) before "
                    "loading this skill. If the tool returns success=true, answer "
                    "from its candidates/evidence and do not load this skill. If "
                    f"the tool returns success=false, call skill_view(name='{name}') before browsing "
                    "this site. Follow the loaded site's Minimal Successful Path "
                    "even when it starts on a redirect target or catalog URL "
                    "instead of the original user-provided URL. Skip every "
                    "anti-route in Do Not Do."
                )
                if has_tool
                else (
                    f"Call skill_view(name='{name}') before browsing this site. "
                    "Follow the loaded site's Minimal Successful Path even when it "
                    "starts on a redirect target or catalog URL instead of the original "
                    "user-provided URL. Skip every anti-route in Do Not Do."
                )
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
        "active_route": state.get("active_route", ACTIVE_ROUTE_SKILL_ONLY),
        "tool_status": state.get("tool_status", TOOL_STATUS_NONE),
        "active_tool_version": state.get("active_tool_version", ""),
        "z_tool_fail_count": int(state.get("z_tool_fail_count", 0) or 0),
        "tool_failure_disable_threshold": get_tool_failure_disable_threshold(),
        "has_tool": has_tool,
        "tool_name": tool_entry.get("tool_name", "") if has_tool else "",
        "tool_path": tool_entry.get("tool_path", "") if has_tool else "",
        "tool_intro": (
            tool_entry.get("description")
            or f"Use dtc_site_search_tool for {normalized} before exploratory browsing."
        ) if has_tool else "",
        "has_skill": False,
        "direct_browse_protocol": direct_browse_protocol,
        "load_instruction": "",
    }


def record_exploration(payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized = normalize_site_url(str(payload.get("site_url") or ""))
    if not urlparse(normalized).netloc:
        return {"success": False, "error": "site_url is required"}
    _merge_legacy_site_state(normalized)
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

    promote_request: Optional[Tuple[int, int, str]] = None
    with _state_lock:
        state = _read_json(state_path, {})
        records: List[Dict[str, Any]] = list(state.get("records") or [])
        payload_route = str(payload.get("route_used") or "").strip()
        payload_tool_success = bool(payload.get("tool_success"))
        records.append({
            "record_id": record_id,
            "sku_id": raw.get("sku_id"),
            "success": bool(raw.get("success")),
            "cleaned_path": str(cleaned_path),
            "created_at": raw.get("created_at"),
            "route_used": payload_route,
            "tool_version": str(payload.get("tool_version") or ""),
            "tool_success": payload_tool_success,
        })
        success_count = int(state.get("success_count", 0) or 0)
        if raw.get("success") and not payload_tool_success:
            success_count += 1
        tool_success_count = int(state.get("tool_success_count", 0) or 0)
        if raw.get("success") and payload_tool_success:
            tool_success_count += 1
        create_threshold = get_success_threshold()
        update_window = get_skill_update_window()
        last_update_count = int(state.get("last_skill_update_success_count", 0) or 0)
        skill_exists = (_skill_dir(skill_name_for_site(site_url)) / "SKILL.md").exists()
        state.update({
            "site_url": site_url,
            "domain": urlparse(site_url).netloc or site_url,
            "site_key": site_key(site_url),
            "skill_name": skill_name_for_site(site_url),
            "success_count": success_count,
            "n_success_count": 0 if skill_exists else max(0, success_count - last_update_count),
            "tool_success_count": tool_success_count,
            "x_success_count": max(0, success_count - last_update_count),
            "success_threshold": create_threshold,
            "skill_update_window": update_window,
            "tool_generation_stable_updates": get_tool_generation_stable_updates(),
            "tool_failure_disable_threshold": get_tool_failure_disable_threshold(),
            "records": records[-100:],
            "updated_at": _utc_now(),
        })
        _ensure_domain_state_shape(state, site_url)
        if raw.get("success") and not payload_tool_success:
            current_skill_status = str(state.get("skill_status") or "")
            current_tool_status = str(state.get("tool_status") or "")
            tool_generation_blocks = (
                current_tool_status in {TOOL_STATUS_BUILDING, TOOL_STATUS_TESTING}
                and not _is_tool_generation_stale(state)
            )
            claimable = (
                current_skill_status not in {"building", "testing", "reviewing"}
                and not tool_generation_blocks
            )
            if not claimable:
                state["n_success_count"] = 0
                state["x_success_count"] = 0
                state["y_no_change_count"] = 0
                state["stable_skill_update_count"] = 0
            if not skill_exists and claimable:
                if success_count >= create_threshold and success_count - last_update_count >= create_threshold:
                    state["skill_status"] = "testing"
                    state["n_success_count"] = 0
                    state["skill_build_started_at"] = _utc_now()
                    state["skill_build_success_count"] = success_count
                    promote_request = (success_count, create_threshold, "create")
            elif skill_exists and claimable:
                if success_count - last_update_count >= update_window:
                    prior_stable_count = int(state.get("stable_skill_update_count", 0) or 0)
                    state["skill_status"] = "reviewing"
                    state["x_success_count"] = 0
                    state["y_no_change_count"] = 0
                    state["stable_skill_update_count"] = 0
                    state["skill_review_started_at"] = _utc_now()
                    state["skill_review_success_count"] = success_count
                    state["skill_review_prior_stable_count"] = prior_stable_count
                    promote_request = (success_count, update_window, "update")
        _bump_state_version(state)
        _write_json(state_path, state)

    if not raw.get("success"):
        return
    if bool(payload.get("tool_success")):
        return

    if promote_request:
        promote_success_count, promote_window, promote_mode = promote_request
        _promote_site_skill(site_url, promote_success_count, promote_window, mode=promote_mode)
        return

    # A generated tool failure is repaired only after a later successful
    # fallback exploration has been cleaned, so the repair prompt has concrete
    # evidence for the route that worked.
    state = _read_json(state_path, {})
    if state.get("pending_tool_failures"):
        _repair_generated_site_tool(site_url, cleaned, state.get("pending_tool_failures") or [])


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


def _parse_json_object(text: str) -> Dict[str, Any]:
    content = (text or "").strip()
    if not content:
        return {}
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _deterministic_skill_creation_eval(content: str, cleaned_chunks: List[str]) -> Dict[str, Any]:
    normalized = _normalize_skill_content(content)
    lower = normalized.lower()
    cleaned_text = "\n\n".join(cleaned_chunks)
    required_sections = [
        "minimal successful path",
        "do not do",
        "product discovery shortcuts",
        "verification hints",
    ]
    missing = [section for section in required_sections if section not in lower]
    has_frontmatter = normalized.startswith("---")
    has_route_signal = any(token in lower for token in ["/products", "search", "site:", "direct", "catalog", "collection"])
    has_negative_signal = any(token in lower for token in ["do not", "avoid", "skip"])
    shorter_than_evidence = len(normalized) < max(1200, int(len(cleaned_text) * 0.85)) if cleaned_text else True
    passed = bool(has_frontmatter and not missing and has_route_signal and has_negative_signal and shorter_than_evidence)
    return {
        "pass": passed,
        "ab_mode": "deterministic_candidate_skill_vs_no_skill",
        "reason": (
            "candidate skill is structured, route-focused, shorter than cleaned evidence, and contains anti-routes"
            if passed
            else "candidate skill did not clearly beat no-skill baseline"
        ),
        "missing_sections": missing,
        "has_frontmatter": has_frontmatter,
        "has_route_signal": has_route_signal,
        "has_negative_signal": has_negative_signal,
        "candidate_chars": len(normalized),
        "cleaned_chars": len(cleaned_text),
        "shorter_than_evidence": shorter_than_evidence,
    }


def _initial_skill_ab_case_from_record(root: Path, rec: Dict[str, Any]) -> Dict[str, Any]:
    record_id = str(rec.get("record_id") or "")
    raw = _read_json(root / "raw" / f"{record_id}.json", {})
    payload = raw.get("payload") or {}
    site_url = normalize_site_url(str(raw.get("site_url") or payload.get("site_url") or ""))
    sku_id = str(raw.get("sku_id") or payload.get("sku_id") or rec.get("sku_id") or "").strip()
    expected_urls = [
        str(url or "").strip()
        for url in (payload.get("product_urls") or [])
        if str(url or "").strip()
    ]
    candidates = payload.get("candidate_products") or []
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("url"):
            expected_urls.append(str(candidate.get("url") or "").strip())
    prompt = (
        f'check if sku_id {sku_id} has a similar or same products in "{site_url}", '
        "if so, provide me with the product link on that website."
    )
    baseline = {
        "record_id": record_id,
        "session_id": str(raw.get("session_id") or payload.get("session_id") or ""),
        "tool_call_id": str(raw.get("tool_call_id") or payload.get("tool_call_id") or ""),
        "api_calls": int(payload.get("api_calls") or raw.get("api_calls") or 0),
        "input_tokens": int(payload.get("input_tokens") or raw.get("input_tokens") or 0),
        "output_tokens": int(payload.get("output_tokens") or raw.get("output_tokens") or 0),
        "total_tokens": int(payload.get("total_tokens") or raw.get("total_tokens") or 0),
        "tool_call_count": int(payload.get("tool_call_count") or raw.get("tool_call_count") or 0),
        "elapsed_seconds": float(payload.get("elapsed_seconds") or raw.get("elapsed_seconds") or 0.0),
    }
    if baseline["total_tokens"] <= 0 and baseline["session_id"]:
        metrics = _session_metrics_from_agent_log(baseline["session_id"])
        for key in ("api_calls", "input_tokens", "output_tokens", "total_tokens"):
            if int(baseline.get(key) or 0) <= 0 and int(metrics.get(key) or 0) > 0:
                baseline[key] = int(metrics.get(key) or 0)
    return {
        "record_id": record_id,
        "sku_id": sku_id,
        "site_url": site_url,
        "prompt": prompt,
        "expected": expected_urls[0] if expected_urls else "none",
        "baseline": baseline,
    }


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


def _build_initial_skill_ab_prompt(case: Dict[str, Any], mode: str, candidate_skill: str = "") -> str:
    common = (
        "Run one stateless DTC same/similar-product search request for A/B evaluation. "
        "Do not rely on prior conversation. Return the final candidate URL if a same/similar "
        "product is found; otherwise say no matching product was found. Do not call "
        "dtc_site_search_record during this A/B evaluation. Start by calling "
        "tiktok_sku_lookup for the sku_id in the user request when present, then "
        "use website search/browsing tools as needed. The candidate skill is guidance, "
        "not an answer key; still verify the current site result with tools.\n\n"
    )
    if mode == "baseline_no_skill":
        policy = (
            "A/B arm: baseline_no_skill. This arm is normally the already recorded "
            "original no-skill search and should not be rerun.\n\n"
        )
    elif mode == "candidate_skill":
        policy = (
            "A/B arm: candidate_skill. Use the candidate SKILL.md instructions below as "
            "if they were loaded by skill_view for this site. Follow its minimal path and "
            "anti-routes before broad exploration.\n\n"
            "Candidate SKILL.md:\n"
            f"{candidate_skill[:16000]}\n\n"
        )
    else:
        raise ValueError(f"unknown initial skill A/B mode: {mode}")
    return common + policy + "User request:\n" + str(case.get("prompt") or "")


def _estimate_prompt_tokens(text: str) -> int:
    # Cheap model-agnostic estimate used only to normalize candidate skill A/B cost.
    return max(0, (len(text or "") + 3) // 4)


def _run_initial_skill_ab_case(case: Dict[str, Any], candidate_skill: str, max_iterations: int = 30) -> Dict[str, Any]:
    from agent.dtc_site_search_ab import run_agent_prompt, score_response

    skill_text = candidate_skill[:16000]
    candidate = run_agent_prompt(
        _build_initial_skill_ab_prompt(case, "candidate_skill", skill_text),
        session_prefix="dtc_skill_ab_candidate",
        max_iterations=max_iterations,
    )
    expected = str(case.get("expected") or "")
    baseline = dict(case.get("baseline") or {})
    candidate_score = score_response(str(candidate.get("final_response") or ""), expected)
    baseline_tokens = int(baseline.get("total_tokens") or 0)
    candidate_tokens = int(candidate.get("total_tokens") or 0)
    skill_token_adjustment = _estimate_prompt_tokens(skill_text)
    adjusted_candidate_tokens = max(1, candidate_tokens - skill_token_adjustment) if candidate_tokens > 0 else 0
    token_reduction_rate = (
        ((baseline_tokens - adjusted_candidate_tokens) / baseline_tokens)
        if baseline_tokens and adjusted_candidate_tokens
        else 0.0
    )
    baseline_tool_calls = int(baseline.get("tool_call_count") or 0)
    candidate_tool_calls = len([e for e in candidate.get("events") or [] if e.get("type") == "tool_complete"])
    if expected:
        same_or_better_output = candidate_score.get("pass") is True
    else:
        same_or_better_output = bool(str(candidate.get("final_response") or "").strip())
    elapsed_delta = float(candidate.get("elapsed_seconds") or 0.0) - float(baseline.get("elapsed_seconds") or 0.0)
    return {
        "record_id": case.get("record_id"),
        "sku_id": case.get("sku_id"),
        "expected": expected,
        "baseline": baseline,
        "candidate": candidate,
        "candidate_score": candidate_score,
        "same_or_better_output": same_or_better_output,
        "candidate_skill_token_adjustment": skill_token_adjustment,
        "raw_candidate_total_tokens": candidate_tokens,
        "adjusted_candidate_total_tokens": adjusted_candidate_tokens,
        "token_delta": adjusted_candidate_tokens - baseline_tokens,
        "token_reduction_rate": token_reduction_rate,
        "tool_call_delta": candidate_tool_calls - baseline_tool_calls,
        "elapsed_delta_seconds": elapsed_delta,
    }


def _evaluate_initial_skill_candidate(site_url: str, content: str, cleaned_chunks: List[str]) -> Dict[str, Any]:
    """Gate first skill publication with recorded A vs candidate-skill B.

    The A arm is the already recorded successful no-skill search, so only the B
    arm is rerun with the candidate skill. Publish only when every case preserves
    the expected output and the batch average total token count is lower.
    """
    root = _site_dir(site_url)
    state = _read_json(root / "state.json", {})
    records = [r for r in state.get("records", []) if r.get("success") and not r.get("tool_success")]
    ab_cases = []
    for rec in records[-max(1, min(len(cleaned_chunks), 3)):]:
        case = _initial_skill_ab_case_from_record(root, rec)
        if case.get("sku_id") and case.get("site_url"):
            ab_cases.append(case)
    deterministic = _deterministic_skill_creation_eval(content, cleaned_chunks)
    if not _skill_creation_ab_enabled():
        deterministic.update({"pass": True, "ab_mode": "disabled_by_config"})
        return deterministic
    if ab_cases:
        try:
            results = [_run_initial_skill_ab_case(case, content) for case in ab_cases]
            judged = len(results)
            passed_output = sum(1 for item in results if item.get("same_or_better_output"))
            token_pairs = []
            for item in results:
                baseline_tokens = int((item.get("baseline") or {}).get("total_tokens") or 0)
                candidate_tokens = int(
                    item.get("adjusted_candidate_total_tokens")
                    or (item.get("candidate") or {}).get("total_tokens")
                    or 0
                )
                if baseline_tokens > 0 and candidate_tokens > 0:
                    token_pairs.append((baseline_tokens, candidate_tokens))
            token_measured_cases = len(token_pairs)
            baseline_avg_tokens = (
                sum(pair[0] for pair in token_pairs) / token_measured_cases
                if token_measured_cases
                else 0.0
            )
            candidate_avg_tokens = (
                sum(pair[1] for pair in token_pairs) / token_measured_cases
                if token_measured_cases
                else 0.0
            )
            average_token_delta = candidate_avg_tokens - baseline_avg_tokens
            efficiency_known = token_measured_cases == judged and judged > 0
            efficiency_pass = efficiency_known and average_token_delta < 0
            passed = judged > 0 and passed_output == judged and efficiency_pass
            return {
                "pass": passed,
                "ab_mode": "recorded_baseline_vs_candidate_skill_rerun",
                "cases": judged,
                "passed_output": passed_output,
                "token_measured_cases": token_measured_cases,
                "baseline_avg_total_tokens": baseline_avg_tokens,
                "candidate_avg_total_tokens": candidate_avg_tokens,
                "average_token_delta": average_token_delta,
                "efficiency_known": efficiency_known,
                "results": results,
                "reason": (
                    "candidate skill rerun preserved output and reduced average total tokens"
                    if passed
                    else "candidate skill rerun did not preserve output or reduce average total tokens"
                ),
                "deterministic_safety": deterministic,
            }
        except Exception as exc:
            logger.info("Initial DTC skill A/B rerun failed for %s", site_url, exc_info=True)
            return {
                "pass": False,
                "ab_mode": "actual_rerun_candidate_skill_vs_no_skill",
                "error": str(exc),
                "reason": "actual rerun A/B failed; candidate skill not published",
                "deterministic_safety": deterministic,
            }
    system = (
        "You evaluate whether a candidate Hermes DTC site-search skill should "
        "be published. Compare the candidate skill against the no-skill baseline "
        "represented by cleaned successful explorations. Pass only if the skill "
        "is clearly more useful than no skill: it preserves the minimal reusable "
        "route, removes one-off product/SKU details, includes material anti-routes, "
        "and should reduce future token/tool cost without lowering recall. Return "
        "one JSON object only."
    )
    user = (
        f"Site URL: {site_url}\n\n"
        f"Candidate SKILL.md:\n{content[:16000]}\n\n"
        "No-skill baseline evidence, cleaned successful explorations:\n"
        + "\n\n---\n\n".join(cleaned_chunks)[:22000]
        + "\n\nReturn JSON with keys: pass(boolean), reason(string), "
        "expected_token_savings(number 0..1), recall_risk(string low|medium|high), "
        "missing_requirements(array)."
    )
    raw = _aux_llm(system, user, "dtc_site_search_skill_creation_ab", max_tokens=900)
    parsed = _parse_json_object(raw)
    if isinstance(parsed.get("pass"), bool):
        parsed.setdefault("ab_mode", "llm_candidate_skill_vs_no_skill")
        parsed.setdefault("deterministic_fallback", deterministic)
        if parsed.get("pass") and not deterministic.get("pass"):
            parsed["pass"] = False
            parsed["reason"] = (
                str(parsed.get("reason") or "")
                + " Deterministic safety check failed; candidate not published."
            ).strip()
        return parsed
    deterministic["llm_evaluation_unavailable"] = True
    return deterministic


def _evaluate_skill_update_candidate(
    site_url: str,
    content: str,
    records: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Gate skill-review changes with recorded old-skill A vs candidate-skill B.

    The A arm is the already completed search that triggered the review, which
    used the active skill version at that time. Only the candidate B arm is
    rerun. Publish the candidate update only if every case preserves output and
    the average adjusted token count is lower.
    """
    root = _site_dir(site_url)
    ab_cases = []
    for rec in records:
        if not rec.get("success") or rec.get("tool_success"):
            continue
        case = _initial_skill_ab_case_from_record(root, rec)
        if case.get("sku_id") and case.get("site_url"):
            ab_cases.append(case)
    if not ab_cases:
        return {
            "pass": False,
            "ab_mode": "recorded_old_skill_vs_candidate_skill_rerun",
            "cases": 0,
            "reason": "no eligible review cases for skill update A/B",
        }
    try:
        results = [_run_initial_skill_ab_case(case, content) for case in ab_cases]
    except Exception as exc:
        logger.info("DTC skill update A/B rerun failed for %s", site_url, exc_info=True)
        return {
            "pass": False,
            "ab_mode": "recorded_old_skill_vs_candidate_skill_rerun",
            "cases": len(ab_cases),
            "error": str(exc),
            "reason": "candidate skill update A/B rerun failed",
        }
    judged = len(results)
    passed_output = sum(1 for item in results if item.get("same_or_better_output"))
    token_pairs = []
    for item in results:
        baseline_tokens = int((item.get("baseline") or {}).get("total_tokens") or 0)
        candidate_tokens = int(
            item.get("adjusted_candidate_total_tokens")
            or (item.get("candidate") or {}).get("total_tokens")
            or 0
        )
        if baseline_tokens > 0 and candidate_tokens > 0:
            token_pairs.append((baseline_tokens, candidate_tokens))
    token_measured_cases = len(token_pairs)
    baseline_avg_tokens = (
        sum(pair[0] for pair in token_pairs) / token_measured_cases
        if token_measured_cases
        else 0.0
    )
    candidate_avg_tokens = (
        sum(pair[1] for pair in token_pairs) / token_measured_cases
        if token_measured_cases
        else 0.0
    )
    average_token_delta = candidate_avg_tokens - baseline_avg_tokens
    efficiency_known = token_measured_cases == judged and judged > 0
    efficiency_pass = efficiency_known and average_token_delta < 0
    passed = judged > 0 and passed_output == judged and efficiency_pass
    return {
        "pass": passed,
        "ab_mode": "recorded_old_skill_vs_candidate_skill_rerun",
        "cases": judged,
        "passed_output": passed_output,
        "token_measured_cases": token_measured_cases,
        "baseline_avg_total_tokens": baseline_avg_tokens,
        "candidate_avg_total_tokens": candidate_avg_tokens,
        "average_token_delta": average_token_delta,
        "efficiency_known": efficiency_known,
        "results": results,
        "reason": (
            "candidate skill update preserved output and reduced average total tokens"
            if passed
            else "candidate skill update did not preserve output or reduce average total tokens"
        ),
    }


def _record_initial_skill_candidate_rejected(
    site_url: str,
    success_count: int,
    evaluation: Dict[str, Any],
) -> None:
    root = _site_dir(site_url)
    with _state_lock:
        state = _read_json(root / "state.json", {})
        state["last_skill_candidate_evaluation"] = evaluation
        state["last_skill_candidate_rejected_at"] = _utc_now()
        state["last_skill_update_success_count"] = success_count
        state["last_skill_update_changed"] = False
        state["skill_status"] = "none"
        state["n_success_count"] = 0
        state["x_success_count"] = 0
        state["y_no_change_count"] = 0
        state["stable_skill_update_count"] = 0
        _ensure_domain_state_shape(state, site_url)
        _bump_state_version(state)
        _write_json(root / "state.json", state)


def _set_skill_status(site_url: str, status: str) -> None:
    root = _site_dir(site_url)
    with _state_lock:
        state = _read_json(root / "state.json", {})
        state["skill_status"] = status
        state["updated_at"] = _utc_now()
        _ensure_domain_state_shape(state, site_url)
        _bump_state_version(state)
        _write_json(root / "state.json", state)


def _promote_site_skill(site_url: str, success_count: int, window: int, mode: str = "update") -> None:
    root = _site_dir(site_url)
    state = _read_json(root / "state.json", {})
    records = [r for r in state.get("records", []) if r.get("success")]
    cleaned_chunks: List[str] = []
    for rec in records[-window:]:
        path = Path(str(rec.get("cleaned_path") or ""))
        try:
            cleaned_chunks.append(path.read_text(encoding="utf-8")[:12000])
        except Exception:
            continue
    if len(cleaned_chunks) < window:
        _set_skill_status(site_url, "active" if (_skill_dir(skill_name_for_site(site_url)) / "SKILL.md").exists() else "none")
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
        "same-item judgment logic. Review passes should be very conservative: "
        "the default action is to return the existing SKILL.md unchanged "
        "byte-for-byte. Do not rewrite wording, reorganize sections, normalize "
        "formatting, rename headings, reorder bullets, or add minor "
        "clarifications just to improve style. Do not change the skill merely "
        "because the latest run used different product examples, different "
        "search terms, or produced extra evidence while following the same "
        "successful route. If the existing SKILL.md has no principled error and "
        "would still lead the agent through the latest case successfully, return "
        "it unchanged. Modify it only when the latest cleaned chains prove a "
        "process failure: an existing instruction is wrong, the minimal route "
        "misses a required durable step, a newly discovered durable route should "
        "replace the old route, or a repeated bad surface needs a material "
        "anti-route to prevent future wasted searches."
    )
    user = (
        f"Site URL: {site_url}\n"
        f"Skill name: {name}\n"
        f"Successful cleaned chain count: {success_count}\n"
        f"Promotion mode: {mode}\n\n"
        f"Existing SKILL.md, if any:\n{existing[:16000]}\n\n"
        "Latest cleaned chains. Extract only reusable website-search lessons; "
        "do not copy the concrete examples into the skill. Collapse them into "
        "the shortest repeatable route and explicit anti-routes:\n\n"
        + "\n\n---\n\n".join(cleaned_chunks)
        + (
            "\n\nReturn a complete SKILL.md with valid YAML frontmatter. "
            "In update mode, first decide whether the existing skill had a "
            "process-level failure. If not, return the existing SKILL.md exactly "
            "as provided, byte-for-byte. "
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
    if (not content or not content.lstrip().startswith("---")) and existing:
        logger.warning(
            "DTC site-search skill update skipped for %s because auxiliary "
            "synthesis failed; keeping existing skill unchanged",
            name,
        )
        _set_skill_status(site_url, "active")
        return
    if not content or not content.lstrip().startswith("---"):
        content = fallback_content

    initial_skill_evaluation: Optional[Dict[str, Any]] = None
    skill_update_evaluation: Optional[Dict[str, Any]] = None
    if mode == "create" and not existing:
        evaluation = _evaluate_initial_skill_candidate(site_url, content, cleaned_chunks)
        if not evaluation.get("pass"):
            _record_initial_skill_candidate_rejected(site_url, success_count, evaluation)
            logger.info(
                "DTC site-search initial skill candidate rejected for %s: %s",
                name,
                evaluation.get("reason"),
            )
            return
        initial_skill_evaluation = evaluation

    changed = _normalize_skill_content(content) != _normalize_skill_content(existing)
    if mode == "update" and existing and changed:
        review_records = records[-max(1, window):]
        skill_update_evaluation = _evaluate_skill_update_candidate(site_url, content, review_records)
        if not skill_update_evaluation.get("pass"):
            logger.info(
                "DTC site-search skill update candidate rejected for %s: %s",
                name,
                skill_update_evaluation.get("reason"),
            )
            content = existing
            changed = False
    if changed:
        wrote = _write_skill_background(name, content, bool(existing))
        if not wrote and content != fallback_content:
            changed = _normalize_skill_content(fallback_content) != _normalize_skill_content(existing)
            wrote = _write_skill_background(name, fallback_content, bool(existing))
        if not wrote:
            _set_skill_status(site_url, "active" if existing else "none")
            return
        try:
            refresh_site_skill_index()
        except Exception:
            logger.debug("Failed to refresh DTC site-search index skill", exc_info=True)

    with _state_lock:
        state = _read_json(root / "state.json", {})
        state["last_skill_update_success_count"] = success_count
        state["last_skill_update_at"] = _utc_now()
        state["last_skill_update_changed"] = changed
        if skill_update_evaluation is not None:
            state["last_skill_update_candidate_evaluation"] = skill_update_evaluation
            if skill_update_evaluation.get("pass"):
                state["last_skill_update_candidate_published_at"] = _utc_now()
            else:
                state["last_skill_update_candidate_rejected_at"] = _utc_now()
        state["success_threshold"] = get_success_threshold()
        state["skill_update_window"] = get_skill_update_window()
        state["tool_generation_stable_updates"] = get_tool_generation_stable_updates()
        state["tool_failure_disable_threshold"] = get_tool_failure_disable_threshold()
        state["n_success_count"] = 0
        state["x_success_count"] = 0
        if mode == "create" and not existing:
            state["skill_status"] = "active"
            state["active_skill_version"] = str(state.get("active_skill_version") or "current")
            if initial_skill_evaluation is not None:
                state["last_skill_candidate_evaluation"] = initial_skill_evaluation
                state["last_skill_candidate_published_at"] = _utc_now()
        elif existing:
            state["skill_status"] = "active"
        if changed:
            state["stable_skill_update_count"] = 0
            state["y_no_change_count"] = 0
            tool_info = dict(state.get("generated_tool") or {})
            if tool_info.get("enabled"):
                tool_info["status"] = "stale_after_skill_change"
                tool_info["enabled"] = False
                tool_info["staled_at"] = _utc_now()
                state["generated_tool"] = tool_info
                state["tool_status"] = TOOL_STATUS_DISABLED
                state["active_route"] = ACTIVE_ROUTE_SKILL_ONLY
                refresh_index_after_write = True
            else:
                refresh_index_after_write = False
        elif existing:
            refresh_index_after_write = False
            prior_stable_count = int(
                state.get("skill_review_prior_stable_count", state.get("stable_skill_update_count", 0)) or 0
            )
            state["stable_skill_update_count"] = prior_stable_count + 1
            state["y_no_change_count"] = int(state.get("stable_skill_update_count", 0) or 0)
        else:
            refresh_index_after_write = False
        _ensure_domain_state_shape(state, site_url)
        _bump_state_version(state)
        _write_json(root / "state.json", state)
    if refresh_index_after_write:
        refresh_generated_tool_index()

    if not changed and existing:
        latest_state = _read_json(root / "state.json", {}) or {}
        stable_count = int(latest_state.get("stable_skill_update_count", 0) or 0)
        generated_tool = latest_state.get("generated_tool") or {}
        if stable_count >= get_tool_generation_stable_updates() and not generated_tool.get("enabled"):
            _generate_site_tool(site_url)

    pending_failures = (_read_json(root / "state.json", {}) or {}).get("pending_tool_failures") or []
    if pending_failures:
        _repair_generated_site_tool(site_url, "\n\n---\n\n".join(cleaned_chunks), pending_failures)


def _fallback_skill(site_url: str, name: str, cleaned_chunks: List[str]) -> str:
    joined = "\n".join(cleaned_chunks)
    lower = joined.lower()
    minimal_notes: List[str] = []
    do_not_notes: List[str] = []
    shortcut_notes: List[str] = []
    verification_notes: List[str] = []

    if "access denied" in lower:
        do_not_notes.append("Do not rely on direct browser navigation if the site returns an Access Denied page.")
    if "web_extract" in lower and ("blocked" in lower or "private or internal" in lower):
        do_not_notes.append("Do not rely on web_extract for this site when it reports blocked/private-network errors.")
    if "site:" in lower and ("web_search" in lower or "search-engine" in lower or "search engine" in lower):
        minimal_notes.append("Use a site-scoped web search with the target product title or distinctive terms when direct site access is blocked.")
        shortcut_notes.append("Prefer exact-title `site:<domain>` queries before broad browsing or homepage exploration.")
    if "/products/" in lower:
        shortcut_notes.append("Prefer direct product-page results under the site's `/products/` URL pattern when available.")
    if "/shop/" in lower:
        shortcut_notes.append("Treat `/shop/` URLs as category or listing evidence unless a direct product page cannot be found.")
    if "user-agent" in lower or "requests.get" in lower:
        minimal_notes.append("If browser tools are blocked, try fetching candidate product URLs with a normal browser User-Agent before giving up.")
    if "image" in lower or "og image" in lower or "variant" in lower:
        verification_notes.append("On product pages, inspect metadata, product numbers, image URLs, and variant images when visible text is insufficient.")

    if not minimal_notes:
        minimal_notes = [
            "Follow the shortest successful route preserved in cleaned explorations.",
            "Prefer direct catalog, collection, product-listing, or product-page URLs over broad homepage exploration.",
        ]
    if not do_not_notes:
        do_not_notes = [
            "Do not repeat failed search boxes, wrong menu clicks, popup interactions, or broad browsing steps from cleaned explorations.",
        ]
    if not shortcut_notes:
        shortcut_notes = [
            "Prefer durable URL patterns, related catalog domains, all-products pages, and listing pages that led to candidates.",
        ]
    if not verification_notes:
        verification_notes = [
            "After reaching a candidate product page, preserve product IDs, canonical URLs, title, size, and image evidence for downstream matching.",
        ]

    minimal = "\n".join(f"- {note}" for note in dict.fromkeys(minimal_notes))
    do_not = "\n".join(f"- {note}" for note in dict.fromkeys(do_not_notes))
    shortcuts = "\n".join(f"- {note}" for note in dict.fromkeys(shortcut_notes))
    verification = "\n".join(f"- {note}" for note in dict.fromkeys(verification_notes))
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
        f"{minimal}\n\n"
        "## Do Not Do\n\n"
        f"{do_not}\n"
        "- Do not preserve one-off SKU IDs, one-off product names, one-off candidate products, or final same-item judgments in this skill.\n\n"
        "## Product Discovery Shortcuts\n\n"
        f"{shortcuts}\n\n"
        +
        "## Verification Hints\n\n"
        f"{verification}\n"
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
        "2. Call `dtc_site_search_context(site_url)`. If it returns `has_tool=true`, call `dtc_site_search_tool(site_url, query, expected_terms)` first and use its structured candidates/evidence.",
        "3. If the generated tool returns `success=true`, do not load the site skill; answer from the tool output and record `tool_success=true`. Only if the generated tool returns `success=false`, and `has_skill=true`, call `skill_view(name=skill_view_name)` before browsing.",
        "4. If a site skill is loaded, follow its `Minimal Successful Path` exactly. That path may start at a redirect target, catalog host, category URL, or product listing URL instead of the user-provided URL.",
        "5. Do not repeat any route, click, search box, snapshot pattern, or broad exploration listed in the loaded skill's `Do Not Do` section.",
        "6. Only when neither generated tool nor site skill exists, use `browser_navigate` on the user's DTC URL, then inspect with `browser_snapshot`, `browser_click`, `browser_type`, and `browser_press`.",
        "7. Only use web search after direct browser exploration fails or reveals that the product catalog lives on a related domain.",
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


def _strip_code_fence(text: str) -> str:
    content = (text or "").strip()
    match = re.search(r"```(?:javascript|js|mjs)?\s*(.*?)```", content, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return content


def _node_bin() -> Optional[str]:
    path_node = shutil.which("node")
    if path_node:
        return path_node
    local_node = REPO_ROOT / ".hermes-node" / "nodejs_wheel" / "bin" / "node"
    if local_node.exists():
        return str(local_node)
    return None


def _test_generated_site_tool(script_path: Path, site_url: str) -> Dict[str, Any]:
    payload = {
        "site_url": site_url,
        "query": "self test",
        "expected_terms": [],
        "max_candidates": 1,
    }
    result = _run_generated_tool_script(script_path, payload, timeout=45)
    if not result.get("success"):
        return result
    parsed = result.get("output") or {}
    if isinstance(parsed, dict) and parsed.get("success") is False and parsed.get("fatal"):
        return {"success": False, "error": str(parsed.get("error") or "Generated tool reported fatal self-test failure")}
    return result


def _run_generated_tool_script(script_path: Path, payload: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    node = _node_bin()
    if not node:
        return {"success": False, "error": "node executable not found"}
    try:
        proc = subprocess.run(
            [node, str(script_path), json.dumps(payload, ensure_ascii=False)],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"success": False, "error": (proc.stderr or proc.stdout or "").strip()[:2000]}
    try:
        parsed = json.loads((proc.stdout or "").strip())
    except Exception:
        return {"success": False, "error": f"Generated tool returned non-JSON output: {(proc.stdout or '')[:1000]}"}
    if not isinstance(parsed, dict):
        return {"success": False, "error": "Generated tool output must be a JSON object"}
    return {"success": True, "output": parsed}


def _normalize_url_for_eval(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme:
        parsed = urlparse("https://" + (url or "").strip())
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", "", ""))


def _candidate_urls_from_tool_output(output: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for candidate in output.get("candidates") or []:
        if isinstance(candidate, dict):
            value = str(candidate.get("url") or "").strip()
            if value:
                urls.append(_normalize_url_for_eval(value))
    return [u for u in urls if u]


def _ab_case_from_raw_record(root: Path, rec: Dict[str, Any]) -> Dict[str, Any]:
    record_id = str(rec.get("record_id") or "")
    raw = _read_json(root / "raw" / f"{record_id}.json", {})
    payload = raw.get("payload") or {}
    candidates = payload.get("candidate_products") or []
    first_candidate = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
    query = " ".join(
        str(x or "").strip()
        for x in [
            first_candidate.get("title"),
            first_candidate.get("style_code"),
            first_candidate.get("manufacturer_style"),
            payload.get("exploration_summary"),
        ]
        if str(x or "").strip()
    )[:500]
    expected_urls = [
        _normalize_url_for_eval(str(url))
        for url in (payload.get("product_urls") or [])
        if str(url or "").strip()
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("url"):
            expected_urls.append(_normalize_url_for_eval(str(candidate.get("url"))))
    expected_terms = []
    for field in ("title", "style_code", "manufacturer_style"):
        value = str(first_candidate.get(field) or "").strip()
        if value:
            expected_terms.append(value)
    return {
        "record_id": record_id,
        "query": query,
        "expected_urls": sorted(set(expected_urls)),
        "expected_terms": expected_terms,
    }


def _evaluate_generated_tool_candidate(script_path: Path, site_url: str, root: Path, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    cases = []
    for rec in records[-5:]:
        case = _ab_case_from_raw_record(root, rec)
        if case["query"] and case["expected_urls"]:
            cases.append(case)
    results = []
    for case in cases:
        run = _run_generated_tool_script(
            script_path,
            {
                "site_url": normalize_site_url(site_url),
                "query": case["query"],
                "expected_terms": case["expected_terms"],
                "max_candidates": 5,
            },
            timeout=75,
        )
        output = run.get("output") if isinstance(run.get("output"), dict) else {}
        candidate_urls = _candidate_urls_from_tool_output(output)
        hit = bool(set(candidate_urls) & set(case["expected_urls"]))
        results.append({
            "record_id": case["record_id"],
            "pass": bool(run.get("success")) and hit,
            "expected_urls": case["expected_urls"],
            "candidate_urls": candidate_urls,
            "tool_success": bool(run.get("success")),
            "error": run.get("error") or output.get("error") if isinstance(output, dict) else run.get("error"),
        })
    judged = len(results)
    passed = sum(1 for item in results if item.get("pass"))
    return {
        "ab_mode": "historical_expected_url",
        "cases": judged,
        "passed": passed,
        "pass_rate": (passed / judged) if judged else None,
        "pass": judged > 0 and passed == judged,
        "results": results,
    }


def _generated_tool_prompt(site_url: str, existing_skill: str, cleaned_chunks: List[str], repair_context: str = "") -> str:
    return (
        "Write a single Node.js ES module script for a Hermes generated DTC site-search tool.\n"
        "Use built-in fetch and lightweight DOM/string/JSON parsing whenever possible. "
        "Only import optional browser automation if it is truly necessary, and avoid "
        "requiring packages that are not already project dependencies. The script must:\n"
        "1. Read one JSON argument from process.argv[2] with keys site_url, query, expected_terms, max_candidates.\n"
        "2. Search the target site for likely product pages using the website-specific strategy from the skill.\n"
        "3. Print exactly one JSON object to stdout and no other text.\n"
        "4. Return {success:true,candidates:[...],evidence:{...},trace:[...]} when it completed, even if candidates is empty.\n"
        "5. Return {success:false,error:string,fatal:true,trace:[...]} only for runtime/tool failures.\n"
        "6. Never add to cart, checkout, log in, or submit forms.\n"
        "7. Keep network timeouts bounded.\n\n"
        f"Site URL: {site_url}\n\n"
        f"Existing SKILL.md:\n{existing_skill[:18000]}\n\n"
        f"Cleaned successful records:\n{chr(10).join(cleaned_chunks)[:22000]}\n\n"
        f"Repair context, if any:\n{repair_context[:10000]}\n\n"
        "Return only JavaScript source code for the .mjs file."
    )


def _write_generated_tool_candidate(
    site_url: str,
    code: str,
    records: List[Dict[str, Any]],
    skill_name: str,
    status: str = "candidate",
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Path, Path]:
    version = _new_version_id()
    version_dir = _generated_tool_versions_dir(site_url) / version
    script_path = version_dir / "site_search_tool.mjs"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = script_path.with_suffix(".mjs.tmp")
    tmp.write_text(code.rstrip() + "\n", encoding="utf-8")
    tmp.replace(script_path)
    metadata = {
        "version": version,
        "site_url": normalize_site_url(site_url),
        "site_key": site_key(site_url),
        "type": "tool",
        "status": status,
        "created_at": _utc_now(),
        "source_records": [str(rec.get("record_id") or "") for rec in records[-max(1, get_success_threshold()):]],
        "base_skill_name": skill_name,
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    _write_json(version_dir / "metadata.json", metadata)
    return version, version_dir, script_path


def _generate_site_tool(site_url: str) -> None:
    root = _site_dir(site_url)
    state_path = root / "state.json"
    state = _read_json(state_path, {})
    if str(state.get("tool_status") or "") in {TOOL_STATUS_BUILDING, TOOL_STATUS_TESTING} and not _is_tool_generation_stale(state):
        return
    name = skill_name_for_site(site_url)
    skill_md = _skill_dir(name) / "SKILL.md"
    if not skill_md.exists():
        _record_tool_generation_failure(state_path, "site skill does not exist")
        return
    _record_tool_generation_started(state_path)
    records = [r for r in state.get("records", []) if r.get("success")]
    cleaned_chunks: List[str] = []
    for rec in records[-max(1, get_success_threshold()):]:
        path = Path(str(rec.get("cleaned_path") or ""))
        try:
            cleaned_chunks.append(path.read_text(encoding="utf-8")[:12000])
        except Exception:
            continue
    try:
        existing_skill = skill_md.read_text(encoding="utf-8")
    except Exception:
        _record_tool_generation_failure(state_path, "failed to read site skill")
        return
    system = (
        "You generate deterministic, auditable website-search automation code "
        "from a Hermes DTC site-search skill. Prefer HTTP/JSON endpoints over "
        "browser automation. The output must be production JavaScript only."
    )
    code = _aux_llm(
        system,
        _generated_tool_prompt(site_url, existing_skill, cleaned_chunks),
        "dtc_site_search_tool_generation",
        max_tokens=4200,
    )
    code = _strip_code_fence(code)
    if not code or "process.argv" not in code or (
        "console.log" not in code and "process.stdout.write" not in code
    ):
        _record_tool_generation_failure(
            state_path,
            "auxiliary tool generation returned empty or invalid JavaScript",
        )
        return
    version, version_dir, script_path = _write_generated_tool_candidate(site_url, code, records, name)
    test_result = _test_generated_site_tool(script_path, site_url)
    repair_attempts: List[Dict[str, Any]] = []
    for attempt in range(1, 3):
        if test_result.get("success"):
            break
        repair_context = (
            f"Generation smoke test failed on attempt {attempt}.\n"
            f"Current version: {version}\n"
            f"Smoke test result:\n{json.dumps(test_result, ensure_ascii=False, indent=2)[:6000]}\n\n"
            f"Current generated code:\n{code[:18000]}"
        )
        repaired = _aux_llm(
            system,
            _generated_tool_prompt(site_url, existing_skill, cleaned_chunks, repair_context),
            "dtc_site_search_tool_generation_repair",
            max_tokens=5200,
        )
        repaired = _strip_code_fence(repaired)
        if not repaired or "process.argv" not in repaired or (
            "console.log" not in repaired and "process.stdout.write" not in repaired
        ):
            repair_attempts.append({
                "attempt": attempt,
                "changed": False,
                "success": False,
                "error": "auxiliary repair returned empty or invalid JavaScript",
                "previous_test_result": test_result,
            })
            break
        changed = repaired.rstrip() != code.rstrip()
        code = repaired
        version, version_dir, script_path = _write_generated_tool_candidate(
            site_url,
            code,
            records,
            name,
            status="candidate_repair",
            extra_metadata={"repair_attempt": attempt},
        )
        test_result = _test_generated_site_tool(script_path, site_url)
        repair_attempts.append({
            "attempt": attempt,
            "changed": changed,
            "success": bool(test_result.get("success")),
            "version": version,
            "test_result": test_result,
        })
        if not changed:
            break
    ab_result = _evaluate_generated_tool_candidate(script_path, site_url, root, records)
    publish_ok = bool(test_result.get("success")) and bool(ab_result.get("pass"))
    evaluation = {
        "version": version,
        "site_url": normalize_site_url(site_url),
        "candidate_type": "tool",
        "status": "approved" if publish_ok else "ab_failed",
        "ab_mode": ab_result.get("ab_mode"),
        "pass": publish_ok,
        "metrics": {
            "smoke_success": bool(test_result.get("success")),
            "candidate_count": len(((test_result.get("output") or {}).get("candidates") or []))
            if isinstance(test_result.get("output"), dict)
            else 0,
            "historical_cases": ab_result.get("cases", 0),
            "historical_pass_rate": ab_result.get("pass_rate"),
        },
        "test_result": test_result,
        "ab_result": ab_result,
        "repair_attempts": repair_attempts,
        "created_at": _utc_now(),
    }
    _write_json(version_dir / "evaluation.json", evaluation)
    with _state_lock:
        state = _read_json(state_path, {})
        state["last_tool_generation_at"] = _utc_now()
        state["last_tool_generation_error"] = "" if publish_ok else (
            str(test_result.get("error") or "generated tool A/B evaluation failed")
        )
        state["generated_tool"] = {
            "enabled": publish_ok,
            "status": "enabled" if publish_ok else "ab_failed",
            "path": str(script_path),
            "version": version,
            "generated_at": _utc_now(),
            "description": (
                "Generated site-specific DTC search tool for "
                f"{normalize_site_url(site_url)}. Call with the target product "
                "query and expected terms to return structured candidate "
                "product pages and evidence."
            ),
            "test_result": test_result,
            "ab_result": ab_result,
            "evaluation_path": str(version_dir / "evaluation.json"),
            "metadata_path": str(version_dir / "metadata.json"),
            "z_tool_fail_count": 0,
        }
        if publish_ok:
            state["tool_status"] = TOOL_STATUS_ACTIVE
            state["active_tool_version"] = version
            state["active_route"] = ACTIVE_ROUTE_TOOL_FIRST
            state["z_tool_fail_count"] = 0
            state["counted_tool_failure_events"] = []
            state["last_tool_disabled_at"] = ""
        else:
            state["tool_status"] = (
                TOOL_STATUS_DISABLED
                if state.get("active_tool_version") and state.get("tool_status") == TOOL_STATUS_ACTIVE
                else TOOL_STATUS_NONE
            )
            state["active_route"] = ACTIVE_ROUTE_SKILL_ONLY
        state["tool_failure_disable_threshold"] = get_tool_failure_disable_threshold()
        _ensure_domain_state_shape(state, site_url)
        _bump_state_version(state)
        _write_json(state_path, state)
    if publish_ok:
        refresh_generated_tool_index()


def _record_tool_generation_failure(state_path: Path, error: str) -> None:
    with _state_lock:
        state = _read_json(state_path, {})
        state["last_tool_generation_at"] = _utc_now()
        state["last_tool_generation_error"] = error
        tool_info = dict(state.get("generated_tool") or {})
        tool_info.update({
            "enabled": False,
            "status": "generation_failed",
            "error": error,
            "generated_at": _utc_now(),
        })
        state["generated_tool"] = tool_info
        state["tool_status"] = (
            TOOL_STATUS_DISABLED
            if state.get("active_tool_version") and state.get("tool_status") == TOOL_STATUS_ACTIVE
            else TOOL_STATUS_NONE
        )
        state["active_route"] = ACTIVE_ROUTE_SKILL_ONLY
        _ensure_domain_state_shape(state, str(state.get("site_url") or ""))
        _bump_state_version(state)
        _write_json(state_path, state)


def _record_tool_generation_started(state_path: Path) -> None:
    with _state_lock:
        state = _read_json(state_path, {})
        now = _utc_now()
        state["last_tool_generation_started_at"] = now
        state["x_success_count"] = 0
        state["y_no_change_count"] = 0
        state["stable_skill_update_count"] = 0
        tool_info = dict(state.get("generated_tool") or {})
        tool_info.update({
            "enabled": False,
            "status": "generating",
            "started_at": now,
        })
        state["generated_tool"] = tool_info
        state["tool_status"] = TOOL_STATUS_BUILDING
        state["active_route"] = ACTIVE_ROUTE_SKILL_ONLY
        _ensure_domain_state_shape(state, str(state.get("site_url") or ""))
        _bump_state_version(state)
        _write_json(state_path, state)


def _repair_generated_site_tool(site_url: str, cleaned_context: str, failures: List[Dict[str, Any]]) -> None:
    root = _site_dir(site_url)
    state_path = root / "state.json"
    state = _read_json(state_path, {})
    name = skill_name_for_site(site_url)
    skill_md = _skill_dir(name) / "SKILL.md"
    tool_info = state.get("generated_tool") or {}
    script_path = Path(str(tool_info.get("path") or "")) if tool_info.get("path") else _generated_tool_path(site_url)
    if not skill_md.exists() or not script_path.exists() or not failures:
        return
    try:
        existing_skill = skill_md.read_text(encoding="utf-8")
        existing_code = script_path.read_text(encoding="utf-8")
    except Exception:
        return
    system = (
        "You repair a generated Hermes DTC site-search Node.js tool after it "
        "failed and a fallback skill-based browser exploration succeeded. "
        "Return the full corrected .mjs source only. If the existing code is "
        "already correct and the failure was transient, return it unchanged."
    )
    repair_context = (
        "Pending generated-tool failures:\n"
        + json.dumps(failures[-5:], ensure_ascii=False, indent=2)[:10000]
        + "\n\nSuccessful fallback cleaned context:\n"
        + cleaned_context[:14000]
        + "\n\nExisting generated tool code:\n"
        + existing_code[:18000]
    )
    code = _aux_llm(
        system,
        _generated_tool_prompt(site_url, existing_skill, [cleaned_context], repair_context),
        "dtc_site_search_tool_repair",
        max_tokens=5200,
    )
    code = _strip_code_fence(code)
    if not code or "process.argv" not in code or (
        "console.log" not in code and "process.stdout.write" not in code
    ):
        return
    changed = code.rstrip() != existing_code.rstrip()
    if changed:
        version = _new_version_id()
        version_dir = _generated_tool_versions_dir(site_url) / version
        script_path = version_dir / "site_search_tool.mjs"
        version_dir.mkdir(parents=True, exist_ok=True)
        tmp = script_path.with_suffix(".mjs.tmp")
        tmp.write_text(code.rstrip() + "\n", encoding="utf-8")
        tmp.replace(script_path)
        _write_json(version_dir / "metadata.json", {
            "version": version,
            "site_url": normalize_site_url(site_url),
            "site_key": site_key(site_url),
            "type": "tool_repair",
            "status": "candidate",
            "created_at": _utc_now(),
            "base_tool_path": str(tool_info.get("path") or ""),
            "failure_count": len(failures),
        })
    else:
        version = str(tool_info.get("version") or "")
        version_dir = script_path.parent
    test_result = _test_generated_site_tool(script_path, site_url)
    _write_json(version_dir / "evaluation.json", {
        "version": version,
        "site_url": normalize_site_url(site_url),
        "candidate_type": "tool_repair",
        "status": "approved" if test_result.get("success") else "smoke_test_failed",
        "ab_mode": "smoke_only",
        "pass": bool(test_result.get("success")),
        "metrics": {
            "smoke_success": bool(test_result.get("success")),
            "candidate_count": len(((test_result.get("output") or {}).get("candidates") or []))
            if isinstance(test_result.get("output"), dict)
            else 0,
        },
        "test_result": test_result,
        "created_at": _utc_now(),
    })
    with _state_lock:
        state = _read_json(state_path, {})
        tool_info = dict(state.get("generated_tool") or {})
        tool_info.update({
            "enabled": bool(test_result.get("success")),
            "status": "enabled" if test_result.get("success") else "repair_test_failed",
            "path": str(script_path),
            "version": version,
            "repaired_at": _utc_now(),
            "description": tool_info.get("description") or (
                "Generated site-specific DTC search tool for "
                f"{normalize_site_url(site_url)}. Call with the target product "
                "query and expected terms to return structured candidate "
                "product pages and evidence."
            ),
            "last_repair_changed": changed,
            "test_result": test_result,
            "evaluation_path": str(version_dir / "evaluation.json"),
            "metadata_path": str(version_dir / "metadata.json"),
            "z_tool_fail_count": 0 if test_result.get("success") else int(tool_info.get("z_tool_fail_count", 0) or 0),
        })
        state["generated_tool"] = tool_info
        if test_result.get("success"):
            state["pending_tool_failures"] = []
            state["tool_status"] = TOOL_STATUS_ACTIVE
            state["active_tool_version"] = version
            state["active_route"] = ACTIVE_ROUTE_TOOL_FIRST
            state["z_tool_fail_count"] = 0
            state["counted_tool_failure_events"] = []
            state["last_tool_disabled_at"] = ""
        else:
            state["tool_status"] = TOOL_STATUS_DISABLED if state.get("tool_status") == TOOL_STATUS_ACTIVE else state.get("tool_status", TOOL_STATUS_NONE)
            state["active_route"] = ACTIVE_ROUTE_SKILL_ONLY
        state["tool_failure_disable_threshold"] = get_tool_failure_disable_threshold()
        _ensure_domain_state_shape(state, site_url)
        _bump_state_version(state)
        _write_json(state_path, state)
    if test_result.get("success"):
        refresh_generated_tool_index()


def record_generated_tool_failure(
    site_url: str,
    query: str,
    result: Dict[str, Any],
    session_id: str = "",
    tool_call_id: str = "",
) -> Dict[str, Any]:
    normalized = normalize_site_url(site_url)
    root = _site_dir(normalized)
    state_path = root / "state.json"
    with _state_lock:
        state = _read_json(state_path, {})
        state = _ensure_domain_state_shape(state, normalized)
        tool_info = dict(state.get("generated_tool") or {})
        active_tool_version = str(state.get("active_tool_version") or tool_info.get("version") or "")
        result_tool_version = str(result.get("tool_version") or active_tool_version or "")
        failure_type = _classify_tool_failure(result)
        event_id = str(result.get("failure_event_id") or _tool_failure_event_id(
            normalized,
            result_tool_version,
            query,
            result,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ))
        failure_event = {
            "event_id": event_id,
            "domain": state.get("domain") or urlparse(normalized).netloc,
            "site_url": normalized,
            "tool_version": result_tool_version,
            "search_record_id": str(result.get("search_record_id") or ""),
            "task_id": str(result.get("task_id") or session_id or ""),
            "tool_call_id": tool_call_id,
            "query": query,
            "failure_type": failure_type,
            "failure_message": str(result.get("error") or result.get("message") or "")[:2000],
            "failed_at": _utc_now(),
        }
        failures = list(state.get("pending_tool_failures") or [])
        failures.append({**failure_event, "result": result})
        state["pending_tool_failures"] = failures[-20:]
        history = list(state.get("tool_failure_history") or [])
        history.append(failure_event)
        state["tool_failure_history"] = history[-200:]

        counted = list(state.get("counted_tool_failure_events") or [])
        already_counted = event_id in counted
        is_current_active_tool = (
            state.get("tool_status") == TOOL_STATUS_ACTIVE
            and state.get("active_route") == ACTIVE_ROUTE_TOOL_FIRST
            and active_tool_version
            and result_tool_version == active_tool_version
        )
        disabled = False
        if is_current_active_tool and not already_counted:
            counted.append(event_id)
            state["counted_tool_failure_events"] = counted[-500:]
            z_count = int(state.get("z_tool_fail_count", 0) or 0) + 1
            state["z_tool_fail_count"] = z_count
            tool_info["z_tool_fail_count"] = z_count
            threshold = get_tool_failure_disable_threshold()
            state["tool_failure_disable_threshold"] = threshold
            if z_count >= threshold:
                disabled = True
                tool_info["enabled"] = False
                tool_info["status"] = TOOL_STATUS_DISABLED
                tool_info["disabled_at"] = _utc_now()
                tool_info["disabled_reason"] = f"z_tool_fail_count reached {threshold}"
                state["tool_status"] = TOOL_STATUS_DISABLED
                state["active_route"] = ACTIVE_ROUTE_SKILL_ONLY
                state["active_tool_version"] = ""
                state["last_tool_disabled_at"] = _utc_now()
                state["last_tool_disabled_version"] = active_tool_version
                state["last_tool_disabled_reason"] = tool_info["disabled_reason"]
                archived = list(state.get("disabled_tool_failure_counts") or [])
                archived.append({
                    "tool_version": active_tool_version,
                    "z_tool_fail_count": z_count,
                    "disabled_at": state["last_tool_disabled_at"],
                    "reason": tool_info["disabled_reason"],
                })
                state["disabled_tool_failure_counts"] = archived[-50:]
                # After disabling a tool, resume from the existing skill phase:
                # do not reset N or remove the skill; restart X/Y review windows.
                state["z_tool_fail_count"] = 0
                tool_info["z_tool_fail_count"] = 0
                state["x_success_count"] = 0
                state["y_no_change_count"] = 0
                state["stable_skill_update_count"] = 0
                refresh_index_after_write = True
            else:
                refresh_index_after_write = False
        else:
            refresh_index_after_write = False
        state["generated_tool"] = tool_info
        _bump_state_version(state)
        _write_json(state_path, state)
    if refresh_index_after_write:
        refresh_generated_tool_index()
    return {
        "counted": bool(is_current_active_tool and not already_counted),
        "duplicate": bool(already_counted),
        "disabled": disabled,
        "z_tool_fail_count": 0 if disabled else int(state.get("z_tool_fail_count", 0) or 0),
        "failure_event_id": event_id,
        "failure_type": failure_type,
        "tool_version": result_tool_version,
    }


def clear_generated_tool_failures(site_url: str) -> None:
    normalized = normalize_site_url(site_url)
    state_path = _site_dir(normalized) / "state.json"
    with _state_lock:
        state = _read_json(state_path, {})
        if not state.get("pending_tool_failures"):
            return
        state["pending_tool_failures"] = []
        state["last_tool_success_at"] = _utc_now()
        # Z is cumulative per active tool version by design; successful calls
        # clear repair backlog but do not clear z_tool_fail_count.
        _ensure_domain_state_shape(state, normalized)
        _bump_state_version(state)
        _write_json(state_path, state)


def run_generated_site_tool(
    site_url: str,
    query: str,
    expected_terms: Optional[List[str]] = None,
    max_candidates: int = 5,
    session_id: str = "",
    tool_call_id: str = "",
) -> Dict[str, Any]:
    normalized = normalize_site_url(site_url)
    state = _read_json(_site_dir(normalized) / "state.json", {})
    state = _ensure_domain_state_shape(state, normalized)
    tool_info = state.get("generated_tool") or {}
    script_path = Path(str(tool_info.get("path") or "")) if tool_info.get("path") else _generated_tool_path(normalized)
    active_tool_version = str(state.get("active_tool_version") or tool_info.get("version") or "")
    if (
        state.get("active_route") != ACTIVE_ROUTE_TOOL_FIRST
        or state.get("tool_status") != TOOL_STATUS_ACTIVE
        or not active_tool_version
        or not tool_info.get("enabled")
        or not script_path.exists()
    ):
        return {
            "success": False,
            "error": "No enabled generated DTC site-search tool exists for this site.",
            "has_tool": False,
            "tool_status": state.get("tool_status", TOOL_STATUS_NONE),
            "active_route": state.get("active_route", ACTIVE_ROUTE_SKILL_ONLY),
        }
    node = _node_bin()
    if not node:
        result = {"success": False, "error": "node executable not found", "has_tool": True, "tool_version": active_tool_version}
        result["failure"] = record_generated_tool_failure(normalized, query, result, session_id=session_id, tool_call_id=tool_call_id)
        return result
    payload = {
        "site_url": normalized,
        "query": query,
        "expected_terms": expected_terms or [],
        "max_candidates": max(1, int(max_candidates or 5)),
    }
    try:
        proc = subprocess.run(
            [node, str(script_path), json.dumps(payload, ensure_ascii=False)],
            cwd=str(REPO_ROOT),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except Exception as exc:
        result = {"success": False, "error": str(exc), "has_tool": True, "tool_version": active_tool_version}
        result["failure"] = record_generated_tool_failure(normalized, query, result, session_id=session_id, tool_call_id=tool_call_id)
        return result
    if proc.returncode != 0:
        result = {
            "success": False,
            "error": (proc.stderr or proc.stdout or f"node exited {proc.returncode}").strip()[:2000],
            "has_tool": True,
            "tool_version": active_tool_version,
        }
        result["failure"] = record_generated_tool_failure(normalized, query, result, session_id=session_id, tool_call_id=tool_call_id)
        return result
    try:
        parsed = json.loads((proc.stdout or "").strip())
    except Exception:
        result = {
            "success": False,
            "error": f"Generated tool returned non-JSON output: {(proc.stdout or '')[:1000]}",
            "has_tool": True,
            "tool_version": active_tool_version,
        }
        result["failure"] = record_generated_tool_failure(normalized, query, result, session_id=session_id, tool_call_id=tool_call_id)
        return result
    if not isinstance(parsed, dict):
        result = {
            "success": False,
            "error": "Generated tool output must be a JSON object",
            "has_tool": True,
            "tool_version": active_tool_version,
        }
        result["failure"] = record_generated_tool_failure(normalized, query, result, session_id=session_id, tool_call_id=tool_call_id)
        return result
    parsed.setdefault("has_tool", True)
    parsed.setdefault("site_url", normalized)
    parsed.setdefault("tool_version", active_tool_version)
    parsed.setdefault("active_route", ACTIVE_ROUTE_TOOL_FIRST)
    if parsed.get("success") and not parsed.get("candidates"):
        parsed["success"] = False
        parsed.setdefault("error", "Generated tool returned no candidates")
    if parsed.get("success") is False:
        parsed["failure"] = record_generated_tool_failure(normalized, query, parsed, session_id=session_id, tool_call_id=tool_call_id)
    elif parsed.get("candidates"):
        clear_generated_tool_failures(normalized)
    return parsed
