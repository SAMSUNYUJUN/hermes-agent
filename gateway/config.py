"""
Gateway configuration management.

Handles loading and validating configuration for:
- Connected platforms (Feishu / Lark)
- Home channels for each platform
- Session reset policies
- Delivery preferences
"""

import logging
import os
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Callable
from enum import Enum

from hermes_cli.config import get_hermes_home
from utils import is_truthy_value

logger = logging.getLogger(__name__)


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce bool-ish config values, preserving a caller-provided default."""
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    return is_truthy_value(value, default=default)


def _coerce_float(value: Any, default: float) -> float:
    """Coerce numeric config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    """Coerce integer config values, falling back on malformed input."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_unauthorized_dm_behavior(value: Any, default: str = "pair") -> str:
    """Normalize unauthorized DM behavior to a supported value."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"pair", "ignore"}:
            return normalized
    return default


def _normalize_notice_delivery(value: Any, default: str = "public") -> str:
    """Normalize notice delivery mode to a supported value."""
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"public", "private"}:
            return normalized
    return default


class Platform(Enum):
    """Known platform identifiers.

    Only Feishu / Lark is connectable in this build. Historical members stay
    here so legacy session metadata can still be parsed safely.
    """
    LOCAL = "local"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    SLACK = "slack"
    SIGNAL = "signal"
    MATTERMOST = "mattermost"
    MATRIX = "matrix"
    HOMEASSISTANT = "homeassistant"
    EMAIL = "email"
    SMS = "sms"
    DINGTALK = "dingtalk"
    API_SERVER = "api_server"
    WEBHOOK = "webhook"
    MSGRAPH_WEBHOOK = "msgraph_webhook"
    FEISHU = "feishu"
    WECOM = "wecom"
    WECOM_CALLBACK = "wecom_callback"
    WEIXIN = "weixin"
    BLUEBUBBLES = "bluebubbles"
    QQBOT = "qqbot"
    YUANBAO = "yuanbao"
    @classmethod
    def _missing_(cls, value):
        """Reject unknown platform names in the Feishu-only build."""
        return None


# Snapshot of built-in platform values before any dynamic _missing_ lookups.
# Used to distinguish real platforms from arbitrary strings.
_BUILTIN_PLATFORM_VALUES = frozenset(m.value for m in Platform.__members__.values())


@dataclass
class HomeChannel:
    """
    Default destination for a platform.
    
    When a cron job specifies deliver="feishu" without a specific chat ID,
    messages are sent to this home channel. Thread-aware platforms may also
    store a thread/topic ID so the bare platform target routes to the exact
    conversation where /sethome was run.
    """
    platform: Platform
    chat_id: str
    name: str  # Human-readable name for display
    thread_id: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "name": self.name,
        }
        if self.thread_id:
            result["thread_id"] = self.thread_id
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HomeChannel":
        return cls(
            platform=Platform(data["platform"]),
            chat_id=str(data["chat_id"]),
            name=data.get("name", "Home"),
            thread_id=str(data["thread_id"]) if data.get("thread_id") else None,
        )


@dataclass
class SessionResetPolicy:
    """
    Controls when sessions reset (lose context).
    
    Modes:
    - "daily": Reset at a specific hour each day
    - "idle": Reset after N minutes of inactivity
    - "both": Whichever triggers first (daily boundary OR idle timeout)
    - "none": Never auto-reset (context managed only by compression)
    """
    mode: str = "both"  # "daily", "idle", "both", or "none"
    at_hour: int = 4  # Hour for daily reset (0-23, local time)
    idle_minutes: int = 1440  # Minutes of inactivity before reset (24 hours)
    notify: bool = True  # Send a notification to the user when auto-reset occurs
    notify_exclude_platforms: tuple = ()
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "at_hour": self.at_hour,
            "idle_minutes": self.idle_minutes,
            "notify": self.notify,
            "notify_exclude_platforms": list(self.notify_exclude_platforms),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionResetPolicy":
        # Handle both missing keys and explicit null values (YAML null → None)
        mode = data.get("mode")
        at_hour = data.get("at_hour")
        idle_minutes = data.get("idle_minutes")
        notify = data.get("notify")
        exclude = data.get("notify_exclude_platforms")
        return cls(
            mode=mode if mode is not None else "both",
            at_hour=at_hour if at_hour is not None else 4,
            idle_minutes=idle_minutes if idle_minutes is not None else 1440,
            notify=_coerce_bool(notify, True),
            notify_exclude_platforms=tuple(exclude) if exclude is not None else (),
        )


@dataclass
class PlatformConfig:
    """Configuration for a single messaging platform."""
    enabled: bool = False
    token: Optional[str] = None
    api_key: Optional[str] = None  # API key if different from token
    home_channel: Optional[HomeChannel] = None
    
    # Reply threading mode.
    # - "off": Never thread replies to original message
    # - "first": Only first chunk threads to user's message (default)
    # - "all": All chunks in multi-part replies thread to user's message
    reply_to_mode: str = "first"

    # Whether the gateway is allowed to send "♻️ Gateway online" /
    # "♻ Gateway restarted" lifecycle notifications on this platform.
    # Default True preserves prior behavior. Set False on platforms used
    # by end users where operator-flavored restart pings are
    # noise; keep True for back-channels where the operator wants them.
    gateway_restart_notification: bool = True

    # Platform-specific settings
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "enabled": self.enabled,
            "extra": self.extra,
            "reply_to_mode": self.reply_to_mode,
            "gateway_restart_notification": self.gateway_restart_notification,
        }
        if self.token:
            result["token"] = self.token
        if self.api_key:
            result["api_key"] = self.api_key
        if self.home_channel:
            result["home_channel"] = self.home_channel.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlatformConfig":
        home_channel = None
        if "home_channel" in data:
            home_channel = HomeChannel.from_dict(data["home_channel"])

        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            token=data.get("token"),
            api_key=data.get("api_key"),
            home_channel=home_channel,
            reply_to_mode=data.get("reply_to_mode", "first"),
            gateway_restart_notification=_coerce_bool(
                data.get("gateway_restart_notification"), True
            ),
            extra=data.get("extra", {}),
        )


# Streaming defaults — single source of truth so both StreamingConfig and
# StreamConsumerConfig agree on the out-of-the-box edit rhythm.
DEFAULT_STREAMING_EDIT_INTERVAL: float = 0.8
DEFAULT_STREAMING_BUFFER_THRESHOLD: int = 24
DEFAULT_STREAMING_CURSOR: str = " ▉"


@dataclass
class StreamingConfig:
    """Configuration for real-time token streaming to messaging platforms."""
    enabled: bool = False
    # Transport selection:
    #   "auto"  — prefer native streaming-draft updates when the platform
    #             supports them; fall back to edit-based when not. Recommended.
    #   "draft" — explicitly request native drafts; falls back to edit when
    #             the platform/chat doesn't support them.
    #   "edit"  — progressive editMessageText only (legacy behaviour).
    #   "off"   — disable streaming entirely.
    transport: str = "auto"
    edit_interval: float = DEFAULT_STREAMING_EDIT_INTERVAL
    buffer_threshold: int = DEFAULT_STREAMING_BUFFER_THRESHOLD
    cursor: str = DEFAULT_STREAMING_CURSOR
    # Ported from openclaw/openclaw#72038.  When >0, the final edit for
    # a long-running streamed response is delivered as a fresh message
    # if the original preview has been visible for at least this many
    # seconds, so the platform's visible timestamp reflects completion
    # time instead of the preview creation time. Set to 0 to disable.
    fresh_final_after_seconds: float = 60.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "transport": self.transport,
            "edit_interval": self.edit_interval,
            "buffer_threshold": self.buffer_threshold,
            "cursor": self.cursor,
            "fresh_final_after_seconds": self.fresh_final_after_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StreamingConfig":
        if not data:
            return cls()
        return cls(
            enabled=_coerce_bool(data.get("enabled"), False),
            transport=data.get("transport", "auto"),
            edit_interval=_coerce_float(
                data.get("edit_interval"), DEFAULT_STREAMING_EDIT_INTERVAL,
            ),
            buffer_threshold=_coerce_int(
                data.get("buffer_threshold"), DEFAULT_STREAMING_BUFFER_THRESHOLD,
            ),
            cursor=data.get("cursor", DEFAULT_STREAMING_CURSOR),
            fresh_final_after_seconds=_coerce_float(
                data.get("fresh_final_after_seconds"), 60.0
            ),
        )


# -----------------------------------------------------------------------------
# Built-in platform connection checkers
# -----------------------------------------------------------------------------
# Each callable receives a ``PlatformConfig`` and returns ``True`` when the
# platform is sufficiently configured to be considered "connected".  Platforms
_PLATFORM_CONNECTED_CHECKERS: dict[Platform, Callable[[PlatformConfig], bool]] = {
    Platform.FEISHU: lambda cfg: bool(cfg.extra.get("app_id")),
}


@dataclass
class GatewayConfig:
    """
    Main gateway configuration.
    
    Manages all platform connections, session policies, and delivery settings.
    """
    # Platform configurations
    platforms: Dict[Platform, PlatformConfig] = field(default_factory=dict)
    
    # Session reset policies by type
    default_reset_policy: SessionResetPolicy = field(default_factory=SessionResetPolicy)
    reset_by_type: Dict[str, SessionResetPolicy] = field(default_factory=dict)
    reset_by_platform: Dict[Platform, SessionResetPolicy] = field(default_factory=dict)
    
    # Reset trigger commands
    reset_triggers: List[str] = field(default_factory=lambda: ["/new", "/reset"])

    # User-defined quick commands (slash commands that bypass the agent loop)
    quick_commands: Dict[str, Any] = field(default_factory=dict)
    
    # Storage paths
    sessions_dir: Path = field(default_factory=lambda: get_hermes_home() / "sessions")
    
    # Delivery settings
    always_log_local: bool = True  # Always save cron outputs to local files

    # STT settings
    stt_enabled: bool = True  # Whether to auto-transcribe inbound voice messages

    # Session isolation in shared chats
    group_sessions_per_user: bool = True  # Isolate group/channel sessions per participant when user IDs are available
    thread_sessions_per_user: bool = False  # When False (default), threads are shared across all participants

    # Unauthorized DM policy
    unauthorized_dm_behavior: str = "pair"  # "pair" or "ignore"

    # Streaming configuration
    streaming: StreamingConfig = field(default_factory=StreamingConfig)

    # Session store pruning: drop SessionEntry records older than this many
    # days from the in-memory dict and sessions.json.  Keeps the store from
    # growing unbounded in gateways serving many chats/threads/users over
    # months.  Pruning is invisible to users — if they resume, they get a
    # fresh session exactly as if the reset policy had fired.  0 = disabled.
    session_store_max_age_days: int = 90

    def get_connected_platforms(self) -> List[Platform]:
        """Return list of platforms that are enabled and configured."""
        connected = []
        for platform, config in self.platforms.items():
            if not config.enabled:
                continue
            if self._is_platform_connected(platform, config):
                connected.append(platform)
        return connected

    def _is_platform_connected(self, platform: Platform, config: PlatformConfig) -> bool:
        """Check whether a single platform is sufficiently configured."""
        if platform != Platform.FEISHU:
            return False

        if config.token or config.api_key:
            return True

        checker = _PLATFORM_CONNECTED_CHECKERS.get(platform)
        return checker(config) if checker is not None else False
    
    def get_home_channel(self, platform: Platform) -> Optional[HomeChannel]:
        """Get the home channel for a platform."""
        config = self.platforms.get(platform)
        if config:
            return config.home_channel
        return None
    
    def get_reset_policy(
        self, 
        platform: Optional[Platform] = None,
        session_type: Optional[str] = None
    ) -> SessionResetPolicy:
        """
        Get the appropriate reset policy for a session.
        
        Priority: platform override > type override > default
        """
        # Platform-specific override takes precedence
        if platform and platform in self.reset_by_platform:
            return self.reset_by_platform[platform]
        
        # Type-specific override (dm, group, thread)
        if session_type and session_type in self.reset_by_type:
            return self.reset_by_type[session_type]
        
        return self.default_reset_policy
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "platforms": {
                p.value: c.to_dict() for p, c in self.platforms.items()
            },
            "default_reset_policy": self.default_reset_policy.to_dict(),
            "reset_by_type": {
                k: v.to_dict() for k, v in self.reset_by_type.items()
            },
            "reset_by_platform": {
                p.value: v.to_dict() for p, v in self.reset_by_platform.items()
            },
            "reset_triggers": self.reset_triggers,
            "quick_commands": self.quick_commands,
            "sessions_dir": str(self.sessions_dir),
            "always_log_local": self.always_log_local,
            "stt_enabled": self.stt_enabled,
            "group_sessions_per_user": self.group_sessions_per_user,
            "thread_sessions_per_user": self.thread_sessions_per_user,
            "unauthorized_dm_behavior": self.unauthorized_dm_behavior,
            "streaming": self.streaming.to_dict(),
            "session_store_max_age_days": self.session_store_max_age_days,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GatewayConfig":
        platforms = {}
        for platform_name, platform_data in data.get("platforms", {}).items():
            try:
                platform = Platform(platform_name)
                if platform != Platform.FEISHU:
                    continue
                platforms[platform] = PlatformConfig.from_dict(platform_data)
            except ValueError:
                pass  # Skip unknown platforms
        
        reset_by_type = {}
        for type_name, policy_data in data.get("reset_by_type", {}).items():
            reset_by_type[type_name] = SessionResetPolicy.from_dict(policy_data)
        
        reset_by_platform = {}
        for platform_name, policy_data in data.get("reset_by_platform", {}).items():
            try:
                platform = Platform(platform_name)
                if platform != Platform.FEISHU:
                    continue
                reset_by_platform[platform] = SessionResetPolicy.from_dict(policy_data)
            except ValueError:
                pass
        
        default_policy = SessionResetPolicy()
        if "default_reset_policy" in data:
            default_policy = SessionResetPolicy.from_dict(data["default_reset_policy"])
        
        sessions_dir = get_hermes_home() / "sessions"
        if "sessions_dir" in data:
            sessions_dir = Path(data["sessions_dir"])
        
        quick_commands = data.get("quick_commands", {})
        if not isinstance(quick_commands, dict):
            quick_commands = {}

        stt_enabled = data.get("stt_enabled")
        if stt_enabled is None:
            stt_enabled = data.get("stt", {}).get("enabled") if isinstance(data.get("stt"), dict) else None

        group_sessions_per_user = data.get("group_sessions_per_user")
        thread_sessions_per_user = data.get("thread_sessions_per_user")
        unauthorized_dm_behavior = _normalize_unauthorized_dm_behavior(
            data.get("unauthorized_dm_behavior"),
            "pair",
        )

        try:
            session_store_max_age_days = int(data.get("session_store_max_age_days", 90))
            session_store_max_age_days = max(session_store_max_age_days, 0)
        except (TypeError, ValueError):
            session_store_max_age_days = 90

        return cls(
            platforms=platforms,
            default_reset_policy=default_policy,
            reset_by_type=reset_by_type,
            reset_by_platform=reset_by_platform,
            reset_triggers=data.get("reset_triggers", ["/new", "/reset"]),
            quick_commands=quick_commands,
            sessions_dir=sessions_dir,
            always_log_local=_coerce_bool(data.get("always_log_local"), True),
            stt_enabled=_coerce_bool(stt_enabled, True),
            group_sessions_per_user=_coerce_bool(group_sessions_per_user, True),
            thread_sessions_per_user=_coerce_bool(thread_sessions_per_user, False),
            unauthorized_dm_behavior=unauthorized_dm_behavior,
            streaming=StreamingConfig.from_dict(data.get("streaming", {})),
            session_store_max_age_days=session_store_max_age_days,
        )

    def get_unauthorized_dm_behavior(self, platform: Optional[Platform] = None) -> str:
        """Return the effective unauthorized-DM behavior for a platform."""
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "unauthorized_dm_behavior" in platform_cfg.extra:
                return _normalize_unauthorized_dm_behavior(
                    platform_cfg.extra.get("unauthorized_dm_behavior"),
                    self.unauthorized_dm_behavior,
                )
        return self.unauthorized_dm_behavior

    def get_notice_delivery(self, platform: Optional[Platform] = None) -> str:
        """Return the effective notice-delivery mode for a platform."""
        if platform:
            platform_cfg = self.platforms.get(platform)
            if platform_cfg and "notice_delivery" in platform_cfg.extra:
                return _normalize_notice_delivery(
                    platform_cfg.extra.get("notice_delivery"),
                    "public",
                )
        return "public"


def load_gateway_config() -> GatewayConfig:
    """
    Load gateway configuration from multiple sources.

    Priority (highest to lowest):
    1. Environment variables
    2. ~/.hermes/config.yaml (primary user-facing config)
    3. ~/.hermes/gateway.json (legacy — provides defaults under config.yaml)
    4. Built-in defaults
    """
    _home = get_hermes_home()
    gw_data: dict = {}

    # Legacy fallback: gateway.json provides the base layer.
    # config.yaml keys always win when both specify the same setting.
    gateway_json_path = _home / "gateway.json"
    if gateway_json_path.exists():
        try:
            with open(gateway_json_path, "r", encoding="utf-8") as f:
                gw_data = json.load(f) or {}
            logger.info(
                "Loaded legacy %s — consider moving settings to config.yaml",
                gateway_json_path,
            )
        except Exception as e:
            logger.warning("Failed to load %s: %s", gateway_json_path, e)

    # Primary source: config.yaml
    try:
        import yaml
        config_yaml_path = _home / "config.yaml"
        if config_yaml_path.exists():
            with open(config_yaml_path, encoding="utf-8") as f:
                yaml_cfg = yaml.safe_load(f) or {}

            # Map config.yaml keys → GatewayConfig.from_dict() schema.
            # Each key overwrites whatever gateway.json may have set.
            sr = yaml_cfg.get("session_reset")
            if sr and isinstance(sr, dict):
                gw_data["default_reset_policy"] = sr

            qc = yaml_cfg.get("quick_commands")
            if qc is not None:
                if isinstance(qc, dict):
                    gw_data["quick_commands"] = qc
                else:
                    logger.warning(
                        "Ignoring invalid quick_commands in config.yaml "
                        "(expected mapping, got %s)",
                        type(qc).__name__,
                    )

            stt_cfg = yaml_cfg.get("stt")
            if isinstance(stt_cfg, dict):
                gw_data["stt"] = stt_cfg

            if "group_sessions_per_user" in yaml_cfg:
                gw_data["group_sessions_per_user"] = yaml_cfg["group_sessions_per_user"]

            if "thread_sessions_per_user" in yaml_cfg:
                gw_data["thread_sessions_per_user"] = yaml_cfg["thread_sessions_per_user"]

            streaming_cfg = yaml_cfg.get("streaming")
            if isinstance(streaming_cfg, dict):
                gw_data["streaming"] = streaming_cfg

            if "reset_triggers" in yaml_cfg:
                gw_data["reset_triggers"] = yaml_cfg["reset_triggers"]

            if "always_log_local" in yaml_cfg:
                gw_data["always_log_local"] = yaml_cfg["always_log_local"]

            if "unauthorized_dm_behavior" in yaml_cfg:
                gw_data["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                    yaml_cfg.get("unauthorized_dm_behavior"),
                    "pair",
                )

            # Merge platforms section from config.yaml into gw_data so that
            # nested keys like platforms.webhook.extra.routes are loaded.
            yaml_platforms = yaml_cfg.get("platforms")
            platforms_data = gw_data.setdefault("platforms", {})
            if not isinstance(platforms_data, dict):
                platforms_data = {}
                gw_data["platforms"] = platforms_data
            if isinstance(yaml_platforms, dict):
                for plat_name, plat_block in yaml_platforms.items():
                    if not isinstance(plat_block, dict):
                        continue
                    existing = platforms_data.get(plat_name, {})
                    if not isinstance(existing, dict):
                        existing = {}
                    # Deep-merge extra dicts so gateway.json defaults survive
                    merged_extra = {**existing.get("extra", {}), **plat_block.get("extra", {})}
                    merged = {**existing, **plat_block}
                    if merged_extra:
                        merged["extra"] = merged_extra
                    platforms_data[plat_name] = merged
                gw_data["platforms"] = platforms_data
            for plat in Platform:
                if plat == Platform.LOCAL:
                    continue
                if plat != Platform.FEISHU:
                    continue
                platform_cfg = yaml_cfg.get(plat.value)
                if not isinstance(platform_cfg, dict):
                    continue
                # Collect bridgeable keys from this platform section
                bridged = {}
                if "unauthorized_dm_behavior" in platform_cfg:
                    bridged["unauthorized_dm_behavior"] = _normalize_unauthorized_dm_behavior(
                        platform_cfg.get("unauthorized_dm_behavior"),
                        gw_data.get("unauthorized_dm_behavior", "pair"),
                    )
                if "notice_delivery" in platform_cfg:
                    bridged["notice_delivery"] = _normalize_notice_delivery(
                        platform_cfg.get("notice_delivery"),
                        "public",
                    )
                if "reply_prefix" in platform_cfg:
                    bridged["reply_prefix"] = platform_cfg["reply_prefix"]
                if "reply_in_thread" in platform_cfg:
                    bridged["reply_in_thread"] = platform_cfg["reply_in_thread"]
                if "require_mention" in platform_cfg:
                    bridged["require_mention"] = platform_cfg["require_mention"]
                if "free_response_channels" in platform_cfg:
                    bridged["free_response_channels"] = platform_cfg["free_response_channels"]
                if "mention_patterns" in platform_cfg:
                    bridged["mention_patterns"] = platform_cfg["mention_patterns"]
                if "dm_policy" in platform_cfg:
                    bridged["dm_policy"] = platform_cfg["dm_policy"]
                if "allow_from" in platform_cfg:
                    bridged["allow_from"] = platform_cfg["allow_from"]
                if "allow_admin_from" in platform_cfg:
                    bridged["allow_admin_from"] = platform_cfg["allow_admin_from"]
                if "user_allowed_commands" in platform_cfg:
                    bridged["user_allowed_commands"] = platform_cfg["user_allowed_commands"]
                if "group_policy" in platform_cfg:
                    bridged["group_policy"] = platform_cfg["group_policy"]
                if "group_allow_from" in platform_cfg:
                    bridged["group_allow_from"] = platform_cfg["group_allow_from"]
                if "group_allow_admin_from" in platform_cfg:
                    bridged["group_allow_admin_from"] = platform_cfg["group_allow_admin_from"]
                if "group_user_allowed_commands" in platform_cfg:
                    bridged["group_user_allowed_commands"] = platform_cfg["group_user_allowed_commands"]
                if "channel_prompts" in platform_cfg:
                    channel_prompts = platform_cfg["channel_prompts"]
                    if isinstance(channel_prompts, dict):
                        bridged["channel_prompts"] = {str(k): v for k, v in channel_prompts.items()}
                    else:
                        bridged["channel_prompts"] = channel_prompts
                enabled_was_explicit = "enabled" in platform_cfg
                if not bridged and not enabled_was_explicit:
                    continue
                plat_data = platforms_data.setdefault(plat.value, {})
                if not isinstance(plat_data, dict):
                    plat_data = {}
                    platforms_data[plat.value] = plat_data
                if enabled_was_explicit:
                    plat_data["enabled"] = platform_cfg["enabled"]
                extra = plat_data.setdefault("extra", {})
                if not isinstance(extra, dict):
                    extra = {}
                    plat_data["extra"] = extra
                extra.update(bridged)

            # Feishu settings → env vars (env vars take precedence)
            feishu_cfg = yaml_cfg.get("feishu", {})
            if isinstance(feishu_cfg, dict):
                if "allow_bots" in feishu_cfg and not os.getenv("FEISHU_ALLOW_BOTS"):
                    os.environ["FEISHU_ALLOW_BOTS"] = str(feishu_cfg["allow_bots"]).lower()

    except Exception as e:
        logger.warning(
            "Failed to process config.yaml — falling back to .env / gateway.json values. "
            "Check %s for syntax errors. Error: %s",
            _home / "config.yaml",
            e,
        )

    config = GatewayConfig.from_dict(gw_data)

    # Override with environment variables
    _apply_env_overrides(config)
    _filter_feishu_platforms(config)
    
    # --- Validate loaded values ---
    _validate_gateway_config(config)

    return config


def _filter_feishu_platforms(config: GatewayConfig) -> None:
    """Drop removed messaging platforms from loaded config."""
    removed = [p.value for p in config.platforms if p != Platform.FEISHU]
    if removed:
        logger.info(
            "Ignoring removed gateway platform(s): %s; only Feishu/Lark is supported.",
            ", ".join(sorted(removed)),
        )
    config.platforms = {
        platform: platform_cfg
        for platform, platform_cfg in config.platforms.items()
        if platform == Platform.FEISHU
    }
    config.reset_by_platform = {
        platform: policy
        for platform, policy in config.reset_by_platform.items()
        if platform == Platform.FEISHU
    }


def _validate_gateway_config(config: "GatewayConfig") -> None:
    """Validate and sanitize a loaded GatewayConfig in place.

    Called by ``load_gateway_config()`` after all config sources are merged.
    Extracted as a separate function for testability.
    """
    policy = config.default_reset_policy

    if not (0 <= policy.at_hour <= 23):
        logger.warning(
            "Invalid at_hour=%s (must be 0-23). Using default 4.", policy.at_hour
        )
        policy.at_hour = 4

    if policy.idle_minutes is None or policy.idle_minutes <= 0:
        logger.warning(
            "Invalid idle_minutes=%s (must be positive). Using default 1440.",
            policy.idle_minutes,
        )
        policy.idle_minutes = 1440

    # Warn about empty bot tokens — platforms that loaded an empty string
    # won't connect and the cause can be confusing without a log line.
    _token_env_names: dict[Platform, str] = {}
    for platform, pconfig in config.platforms.items():
        if not pconfig.enabled:
            continue
        env_name = _token_env_names.get(platform)
        if env_name and pconfig.token is not None and not pconfig.token.strip():
            logger.warning(
                "%s is enabled but %s is empty. "
                "The adapter will likely fail to connect.",
                platform.value, env_name,
            )

    # Reject known-weak placeholder tokens.
    # Ported from openclaw/openclaw#64586: users who copy .env.example
    # without changing placeholder values get a clear startup error instead
    # of a confusing "auth failed" from the platform API.
    try:
        from hermes_cli.auth import has_usable_secret
    except ImportError:
        has_usable_secret = None  # type: ignore[assignment]

    if has_usable_secret is not None:
        for platform, pconfig in config.platforms.items():
            if not pconfig.enabled:
                continue
            env_name = _token_env_names.get(platform)
            if not env_name:
                continue
            token = pconfig.token
            if token and token.strip() and not has_usable_secret(token, min_length=4):
                logger.error(
                    "%s is enabled but %s is set to a placeholder value ('%s'). "
                    "Set a real bot token before starting the gateway. "
                    "The adapter will NOT be started.",
                    platform.value, env_name, token.strip()[:6] + "...",
                )
                pconfig.enabled = False


def _apply_env_overrides(config: GatewayConfig) -> None:
    """Apply environment variable overrides to config."""

    # Feishu / Lark
    feishu_app_id = os.getenv("FEISHU_APP_ID")
    feishu_app_secret = os.getenv("FEISHU_APP_SECRET")
    if feishu_app_id and feishu_app_secret:
        if Platform.FEISHU not in config.platforms:
            config.platforms[Platform.FEISHU] = PlatformConfig()
        config.platforms[Platform.FEISHU].enabled = True
        config.platforms[Platform.FEISHU].extra.update({
            "app_id": feishu_app_id,
            "app_secret": feishu_app_secret,
            "domain": os.getenv("FEISHU_DOMAIN", "feishu"),
            "connection_mode": os.getenv("FEISHU_CONNECTION_MODE", "websocket"),
        })
        feishu_encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "")
        if feishu_encrypt_key:
            config.platforms[Platform.FEISHU].extra["encrypt_key"] = feishu_encrypt_key
        feishu_verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
        if feishu_verification_token:
            config.platforms[Platform.FEISHU].extra["verification_token"] = feishu_verification_token
        feishu_home = os.getenv("FEISHU_HOME_CHANNEL")
        if feishu_home:
            config.platforms[Platform.FEISHU].home_channel = HomeChannel(
                platform=Platform.FEISHU,
                chat_id=feishu_home,
                name=os.getenv("FEISHU_HOME_CHANNEL_NAME", "Home"),
                thread_id=os.getenv("FEISHU_HOME_CHANNEL_THREAD_ID") or None,
            )

    # Session settings
    idle_minutes = os.getenv("SESSION_IDLE_MINUTES")
    if idle_minutes:
        try:
            config.default_reset_policy.idle_minutes = int(idle_minutes)
        except ValueError:
            pass
    
    reset_hour = os.getenv("SESSION_RESET_HOUR")
    if reset_hour:
        try:
            config.default_reset_policy.at_hour = int(reset_hour)
        except ValueError:
            pass
