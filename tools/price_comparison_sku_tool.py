#!/usr/bin/env python3
"""TikTok SKU product-detail extraction tool for same-item matching workflows."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error, tool_result


_DEFAULT_FETCH_MODULE_PATH = (
    "/mnt/bn/zhangwendong-nas06/xianyang/agent_price_comparison/"
    "util/fetch_index_feature.py"
)
_SKU_ID_RE = re.compile(r"^[0-9]{1,32}$")
_FETCH_MODULE: ModuleType | None = None
_DEFAULT_FETCH_RETRIES = 3
_DEFAULT_RETRY_DELAY_SECONDS = 0.5


def _fetch_module_path() -> Path:
    return Path(
        os.getenv("PRICE_COMPARISON_FETCH_INDEX_FEATURE", _DEFAULT_FETCH_MODULE_PATH)
    )


def _cache_dir() -> Path:
    return get_hermes_home() / "cache" / "price_comparison_sku_features"


def _load_fetch_module() -> ModuleType:
    """Load the existing fetch_index_feature.py module lazily."""
    global _FETCH_MODULE
    if _FETCH_MODULE is not None:
        return _FETCH_MODULE

    module_path = _fetch_module_path()
    if not module_path.exists():
        raise FileNotFoundError(f"fetch_index_feature.py not found at {module_path}")

    spec = importlib.util.spec_from_file_location(
        "price_comparison_fetch_index_feature", str(module_path)
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _FETCH_MODULE = module
    return module


def _validate_sku_id(sku_id: str) -> str:
    sku_id = str(sku_id or "").strip()
    if not _SKU_ID_RE.fullmatch(sku_id):
        raise ValueError("sku_id must be a numeric TikTok SKU id string")
    return sku_id


def _normalize_raw_sku_info(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise TypeError(f"expected SKU info dict, got {type(raw).__name__}")
    return raw


def _fetch_retries() -> int:
    raw = os.getenv("TIKTOK_SKU_LOOKUP_RETRIES", "").strip()
    if not raw:
        return _DEFAULT_FETCH_RETRIES
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_FETCH_RETRIES


def _retry_delay_seconds() -> float:
    raw = os.getenv("TIKTOK_SKU_LOOKUP_RETRY_DELAY_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_RETRY_DELAY_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_RETRY_DELAY_SECONDS


def _is_transient_fetch_error(exc: Exception) -> bool:
    message = str(exc)
    transient_markers = (
        "GaussSlave Service",
        "Error code is: -106",
        "StatusCode",
        "timeout",
        "timed out",
        "temporarily",
        "connection",
    )
    return any(marker.lower() in message.lower() for marker in transient_markers)


@contextmanager
def _suppress_external_logging_format_errors():
    """Hide third-party logging formatter bugs while preserving real exceptions."""
    previous = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        yield
    finally:
        logging.raiseExceptions = previous


def _call_fetch_module(module: ModuleType, sku_id: str, cache_dir: Path) -> Any:
    if hasattr(module, "get_sku_info_with_cache"):
        return module.get_sku_info_with_cache(sku_id, str(cache_dir))
    if hasattr(module, "get_sku_info"):
        raw = module.get_sku_info(sku_id)
        cache_path = cache_dir / f"{sku_id}.json"
        cache_path.write_text(
            json.dumps(_normalize_raw_sku_info(raw), ensure_ascii=False),
            encoding="utf-8",
        )
        return raw
    raise AttributeError("fetch module lacks get_sku_info_with_cache/get_sku_info")


def fetch_tiktok_sku_full_info(sku_id: str) -> Dict[str, Any]:
    """Fetch all configured TikTok SKU index features and cache the full dict."""
    sku_id = _validate_sku_id(sku_id)
    cache_dir = _cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    module = _load_fetch_module()
    attempts = _fetch_retries()
    delay = _retry_delay_seconds()
    last_exc: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            with _suppress_external_logging_format_errors():
                raw = _call_fetch_module(module, sku_id, cache_dir)
            return _normalize_raw_sku_info(raw)
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not _is_transient_fetch_error(exc):
                break
            time.sleep(delay * attempt)

    assert last_exc is not None
    raise RuntimeError(
        f"Failed to fetch TikTok SKU {sku_id} after {attempts} attempt(s): {last_exc}"
    ) from last_exc


def sku_info_for_agent(sku_id: str) -> Dict[str, Any]:
    """Return compact product-detail fields intended for same-item matching."""
    sku_id = _validate_sku_id(sku_id)
    full_info = fetch_tiktok_sku_full_info(sku_id)
    return {
        "success": True,
        "sku_id": sku_id,
        "title": full_info.get("g_ecom_pc_spu_name", ""),
        "description": full_info.get("g_ecom_pc_spu_desc_text", ""),
        "sku_images": full_info.get("g_ecom_pc_sku_imgs", []) or [],
    }


def tiktok_sku_lookup(sku_id: str) -> str:
    """Tool handler: fetch/cache full SKU info, return product-detail payload."""
    try:
        return tool_result(sku_info_for_agent(sku_id))
    except Exception as exc:
        return tool_error(str(exc), success=False, sku_id=str(sku_id or "").strip())


def check_price_comparison_requirements() -> bool:
    """Return True when the local internal dependencies are importable."""
    if not _fetch_module_path().exists():
        return False
    try:
        import euler  # noqa: F401
        import bytedtbase  # noqa: F401
        import snappy  # noqa: F401
        import zstandard  # noqa: F401
    except Exception:
        return False
    return True


TIKTOK_SKU_LOOKUP_SCHEMA = {
    "name": "tiktok_sku_lookup",
    "description": (
        "Extract product details for a TikTok SKU id from the internal SKU index. "
        "Use this to get TikTok-side title, description, and SKU image URLs for "
        "later same-item matching against products from other websites. "
        "This tool does not compare prices and does not decide whether items match. "
        "Caches the complete raw feature dict under HERMES_HOME/cache, but "
        "returns only the product title, description, and SKU image URLs."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sku_id": {
                "type": "string",
                "description": "Numeric TikTok SKU id, for example 7120033486086343941.",
            },
        },
        "required": ["sku_id"],
        "additionalProperties": False,
    },
}


registry.register(
    name="tiktok_sku_lookup",
    toolset="tiktok_sku",
    schema=TIKTOK_SKU_LOOKUP_SCHEMA,
    handler=lambda args, **kw: tiktok_sku_lookup(args.get("sku_id", "")),
    check_fn=check_price_comparison_requirements,
    emoji="🛍️",
)
