"""
Shared platform registry for Hermes Agent.

Single source of truth for platform metadata consumed by both
skills_config (label display) and tools_config (default toolset
resolution).  Import ``PLATFORMS`` from here instead of maintaining
duplicate dicts in each module.
"""

from collections import OrderedDict
from typing import NamedTuple


class PlatformInfo(NamedTuple):
    """Metadata for a single platform entry."""
    label: str
    default_toolset: str


# Ordered so that TUI menus are deterministic.
PLATFORMS: OrderedDict[str, PlatformInfo] = OrderedDict([
    ("cli",            PlatformInfo(label="🖥️  CLI",            default_toolset="hermes-cli")),
    ("feishu",         PlatformInfo(label="🪽 Feishu",          default_toolset="hermes-feishu")),
    ("cron",           PlatformInfo(label="⏰ Cron",            default_toolset="hermes-cron")),
])


def platform_label(key: str, default: str = "") -> str:
    """Return the display label for a platform key, or *default*."""
    info = PLATFORMS.get(key)
    if info is not None:
        return info.label
    return default


def get_all_platforms() -> "OrderedDict[str, PlatformInfo]":
    """Return the static Feishu-only platform list."""
    return OrderedDict(PLATFORMS)
