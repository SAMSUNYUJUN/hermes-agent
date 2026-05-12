"""Send Message Tool -- Feishu/Lark messaging only."""

import json
import logging
import os
import re

from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)

_FEISHU_TARGET_RE = re.compile(
    r"^\s*((?:oc|ou|on|chat|open)_[-A-Za-z0-9]+)(?::([-A-Za-z0-9_]+))?\s*$"
)
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
_VOICE_EXTS = {".ogg", ".opus"}
_URL_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|api[_-]?key|auth[_-]?token|token|signature|sig)=)([^&#\s]+)",
    re.IGNORECASE,
)
_GENERIC_SECRET_ASSIGN_RE = re.compile(
    r"\b(access_token|api[_-]?key|auth[_-]?token|signature|sig)\s*=\s*([^\s,;]+)",
    re.IGNORECASE,
)


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": (
        "Send a message to Feishu/Lark, or list known Feishu/Lark targets.\n\n"
        "IMPORTANT: When the user asks to send to a specific chat or person "
        "(not just the bare platform name), call send_message(action='list') "
        "FIRST to see available targets, then send to the correct one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "list"],
                "description": "'send' sends a message. 'list' returns known Feishu/Lark targets.",
            },
            "target": {
                "type": "string",
                "description": (
                    "Delivery target. Use 'feishu' for the configured home chat, "
                    "'feishu:<chat_id>' for a chat, or "
                    "'feishu:<chat_id>:<thread_id>' for a thread."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Message text to send. To send an image or file, include "
                    "MEDIA:<local_path> in the message."
                ),
            },
        },
        "required": [],
    },
}


def _sanitize_error_text(text) -> str:
    """Redact secrets from error text before surfacing it to users/models."""
    redacted = redact_sensitive_text(text)
    redacted = _URL_SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}***", redacted)
    redacted = _GENERIC_SECRET_ASSIGN_RE.sub(lambda m: f"{m.group(1)}=***", redacted)
    return redacted


def _error(message: str) -> dict:
    return {"error": _sanitize_error_text(message)}


def send_message_tool(args, **kw):
    """Handle Feishu/Lark send_message tool calls."""
    action = args.get("action", "send")
    if action == "list":
        return _handle_list()
    return _handle_send(args)


def _handle_list():
    """Return known Feishu/Lark messaging targets from the channel directory."""
    try:
        from gateway.channel_directory import DIRECTORY_PATH

        if not DIRECTORY_PATH.exists():
            return json.dumps({"targets": []})
        data = json.loads(DIRECTORY_PATH.read_text(encoding="utf-8"))
        feishu_entries = (data.get("platforms") or {}).get("feishu", [])
        targets = []
        for entry in feishu_entries if isinstance(feishu_entries, list) else []:
            chat_id = entry.get("id")
            if not chat_id:
                continue
            name = entry.get("name") or chat_id
            thread_id = entry.get("thread_id")
            target = f"feishu:{chat_id}:{thread_id}" if thread_id else f"feishu:{chat_id}"
            targets.append({"target": target, "name": name})
        return json.dumps({"targets": targets})
    except Exception as e:
        return json.dumps(_error(f"Failed to load Feishu channel directory: {e}"))


def _handle_send(args):
    """Send a message to a Feishu/Lark target."""
    target = args.get("target", "")
    message = args.get("message", "")
    if not target or not message:
        return tool_error("Both 'target' and 'message' are required when action='send'")

    parts = target.split(":", 1)
    platform_name = parts[0].strip().lower()
    if platform_name not in {"feishu", "lark"}:
        return tool_error("Only Feishu/Lark messaging is supported.")

    target_ref = parts[1].strip() if len(parts) > 1 else None
    chat_id = None
    thread_id = None
    is_explicit = False
    if target_ref:
        chat_id, thread_id, is_explicit = _parse_target_ref(target_ref)

    if target_ref and not is_explicit:
        try:
            from gateway.channel_directory import resolve_channel_name

            resolved = resolve_channel_name("feishu", target_ref)
            if resolved:
                chat_id, thread_id, _ = _parse_target_ref(resolved)
            else:
                return json.dumps({
                    "error": (
                        f"Could not resolve '{target_ref}' on Feishu. "
                        "Use send_message(action='list') to see available targets."
                    )
                })
        except Exception:
            return json.dumps({
                "error": (
                    f"Could not resolve '{target_ref}' on Feishu. "
                    "Try using a Feishu chat ID instead."
                )
            })

    from tools.interrupt import is_interrupted
    if is_interrupted():
        return tool_error("Interrupted")

    try:
        from gateway.config import Platform, load_gateway_config

        config = load_gateway_config()
        platform = Platform.FEISHU
    except Exception as e:
        return json.dumps(_error(f"Failed to load gateway config: {e}"))

    pconfig = config.platforms.get(platform)
    if not pconfig or not pconfig.enabled:
        return tool_error(
            "Feishu is not configured. Set FEISHU_APP_ID and FEISHU_APP_SECRET "
            "or configure the feishu platform in ~/.hermes/config.yaml."
        )

    from gateway.platforms.base import BasePlatformAdapter

    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    mirror_text = cleaned_message.strip() or _describe_media_for_mirror(media_files)

    used_home_channel = False
    if not chat_id:
        home = config.get_home_channel(platform)
        if home:
            chat_id = home.chat_id
            thread_id = thread_id or home.thread_id
            used_home_channel = True
        else:
            return json.dumps({
                "error": (
                    "No Feishu home channel is set. Use 'feishu:<chat_id>' "
                    "or set FEISHU_HOME_CHANNEL."
                )
            })

    duplicate_skip = _maybe_skip_cron_duplicate_send("feishu", chat_id, thread_id)
    if duplicate_skip:
        return json.dumps(duplicate_skip)

    try:
        from model_tools import _run_async

        result = _run_async(
            _send_to_feishu(
                pconfig,
                chat_id,
                cleaned_message,
                thread_id=thread_id,
                media_files=media_files,
            )
        )
        if used_home_channel and isinstance(result, dict) and result.get("success"):
            result["note"] = f"Sent to Feishu home channel (chat_id: {chat_id})"

        if isinstance(result, dict) and result.get("success") and mirror_text:
            try:
                from gateway.mirror import mirror_to_session
                from gateway.session_context import get_session_env

                source_label = get_session_env("HERMES_SESSION_PLATFORM", "cli")
                user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
                if mirror_to_session(
                    "feishu",
                    chat_id,
                    mirror_text,
                    source_label=source_label,
                    thread_id=thread_id,
                    user_id=user_id,
                ):
                    result["mirrored"] = True
            except Exception:
                pass

        if isinstance(result, dict) and "error" in result:
            result["error"] = _sanitize_error_text(result["error"])
        return json.dumps(result)
    except Exception as e:
        return json.dumps(_error(f"Send failed: {e}"))


def _parse_target_ref(target_ref: str):
    match = _FEISHU_TARGET_RE.fullmatch(target_ref)
    if match:
        return match.group(1), match.group(2), True
    return None, None, False


def _describe_media_for_mirror(media_files):
    if not media_files:
        return ""
    if len(media_files) == 1:
        media_path, is_voice = media_files[0]
        ext = os.path.splitext(media_path)[1].lower()
        if is_voice and ext in _VOICE_EXTS:
            return "[Sent voice message]"
        if ext in _IMAGE_EXTS:
            return "[Sent image attachment]"
        if ext in _VIDEO_EXTS:
            return "[Sent video attachment]"
        if ext in _AUDIO_EXTS:
            return "[Sent audio attachment]"
        return "[Sent document attachment]"
    return f"[Sent {len(media_files)} media attachments]"


def _get_cron_auto_delivery_target():
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip().lower()
    chat_id = get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip()
    if not platform or not chat_id:
        return None
    thread_id = get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "").strip() or None
    return {"platform": platform, "chat_id": chat_id, "thread_id": thread_id}


def _maybe_skip_cron_duplicate_send(platform_name: str, chat_id: str, thread_id: str | None):
    auto_target = _get_cron_auto_delivery_target()
    if not auto_target:
        return None
    same_target = (
        auto_target["platform"] == platform_name
        and str(auto_target["chat_id"]) == str(chat_id)
        and auto_target.get("thread_id") == thread_id
    )
    if not same_target:
        return None

    target_label = f"{platform_name}:{chat_id}"
    if thread_id is not None:
        target_label += f":{thread_id}"
    return {
        "success": True,
        "skipped": True,
        "reason": "cron_auto_delivery_duplicate_target",
        "target": target_label,
        "note": (
            f"Skipped send_message to {target_label}. This cron job will already "
            "auto-deliver its final response to that same target."
        ),
    }


async def _send_to_feishu(pconfig, chat_id, message, *, thread_id=None, media_files=None):
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.feishu import FEISHU_AVAILABLE, FeishuAdapter

    if not FEISHU_AVAILABLE:
        return {"error": "Feishu dependencies not installed. Run: pip install 'hermes-agent[feishu]'"}

    media_files = media_files or []
    max_len = FeishuAdapter.MAX_MESSAGE_LENGTH
    chunks = BasePlatformAdapter.truncate_message(message, max_len) if max_len else [message]

    last_result = None
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        result = await _send_feishu(
            pconfig,
            chat_id,
            chunk,
            media_files=media_files if is_last else None,
            thread_id=thread_id,
        )
        if isinstance(result, dict) and result.get("error"):
            return result
        last_result = result
    return last_result


async def _send_feishu(pconfig, chat_id, message, media_files=None, thread_id=None):
    """Send via Feishu/Lark using the adapter's send pipeline."""
    try:
        from gateway.platforms.feishu import FEISHU_AVAILABLE, FEISHU_DOMAIN, LARK_DOMAIN, FeishuAdapter

        if not FEISHU_AVAILABLE:
            return {"error": "Feishu dependencies not installed. Run: pip install 'hermes-agent[feishu]'"}
    except ImportError:
        return {"error": "Feishu dependencies not installed. Run: pip install 'hermes-agent[feishu]'"}

    media_files = media_files or []
    try:
        adapter = FeishuAdapter(pconfig)
        domain_name = getattr(adapter, "_domain_name", "feishu")
        domain = FEISHU_DOMAIN if domain_name != "lark" else LARK_DOMAIN
        adapter._client = adapter._build_lark_client(domain)
        metadata = {"thread_id": thread_id} if thread_id else None

        last_result = None
        if message.strip():
            last_result = await adapter.send(chat_id, message, metadata=metadata)
            if not last_result.success:
                return _error(f"Feishu send failed: {last_result.error}")

        for media_path, is_voice in media_files:
            if not os.path.exists(media_path):
                return _error(f"Media file not found: {media_path}")

            ext = os.path.splitext(media_path)[1].lower()
            if ext in _IMAGE_EXTS:
                last_result = await adapter.send_image_file(chat_id, media_path, metadata=metadata)
            elif ext in _VIDEO_EXTS:
                last_result = await adapter.send_video(chat_id, media_path, metadata=metadata)
            elif ext in _VOICE_EXTS and is_voice:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            elif ext in _AUDIO_EXTS:
                last_result = await adapter.send_voice(chat_id, media_path, metadata=metadata)
            else:
                last_result = await adapter.send_document(chat_id, media_path, metadata=metadata)

            if not last_result.success:
                return _error(f"Feishu media send failed: {last_result.error}")

        if last_result is None:
            return {"error": "No deliverable text or media remained after processing MEDIA tags"}

        return {
            "success": True,
            "platform": "feishu",
            "chat_id": chat_id,
            "message_id": last_result.message_id,
        }
    except Exception as e:
        return _error(f"Feishu send failed: {e}")


def _check_send_message():
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    from gateway.session_context import get_session_env

    platform = get_session_env("HERMES_SESSION_PLATFORM", "")
    if platform == "feishu":
        return True
    try:
        from gateway.status import is_gateway_running

        return is_gateway_running()
    except Exception:
        return False


from tools.registry import registry, tool_error

registry.register(
    name="send_message",
    toolset="messaging",
    schema=SEND_MESSAGE_SCHEMA,
    handler=send_message_tool,
    check_fn=_check_send_message,
    emoji="📨",
)
