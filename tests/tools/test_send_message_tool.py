"""Tests for the Feishu-only send_message tool."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from tools.send_message_tool import _parse_target_ref, send_message_tool


def _run_async_immediately(coro):
    import asyncio

    return asyncio.run(coro)


def test_rejects_non_feishu_targets():
    result = json.loads(
        send_message_tool(
            {"action": "send", "target": "telegram:123", "message": "hello"}
        )
    )

    assert result["error"] == "Only Feishu/Lark messaging is supported."


def test_parse_feishu_target_with_thread():
    assert _parse_target_ref("oc_abc123:thread_456") == (
        "oc_abc123",
        "thread_456",
        True,
    )


def test_list_returns_only_feishu_targets(tmp_path):
    cache_file = tmp_path / "channel_directory.json"
    cache_file.write_text(
        json.dumps(
            {
                "platforms": {
                    "feishu": [{"id": "oc_abc123", "name": "Ops"}],
                    "telegram": [{"id": "123", "name": "Old"}],
                }
            }
        ),
        encoding="utf-8",
    )

    with patch("gateway.channel_directory.DIRECTORY_PATH", cache_file):
        result = json.loads(send_message_tool({"action": "list"}))

    assert result == {"targets": [{"target": "feishu:oc_abc123", "name": "Ops"}]}


def test_send_uses_feishu_platform_and_mirrors():
    pconfig = SimpleNamespace(enabled=True, extra={"app_id": "cli_a"})
    config = SimpleNamespace(
        platforms={Platform.FEISHU: pconfig},
        get_home_channel=lambda _platform: None,
    )

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("model_tools._run_async", side_effect=_run_async_immediately), \
         patch(
             "tools.send_message_tool._send_to_feishu",
             new=AsyncMock(return_value={"success": True, "chat_id": "oc_abc123"}),
         ) as send_mock, \
         patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
        result = json.loads(
            send_message_tool(
                {
                    "action": "send",
                    "target": "feishu:oc_abc123",
                    "message": "hello",
                }
            )
        )

    assert result["success"] is True
    assert result["mirrored"] is True
    send_mock.assert_awaited_once()
    mirror_mock.assert_called_once()
