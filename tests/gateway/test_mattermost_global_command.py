"""Tests for the Mattermost /hermes router."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.mattermost.adapter import MattermostAdapter, _apply_yaml_config


COMMAND_TOKEN = "command-token"
BOT_TOKEN = "bot-token"


class FakeRequest:
    def __init__(self, form, *, token=COMMAND_TOKEN):
        self._form = form
        self.headers = {"Authorization": f"Token {token}"}

    async def post(self):
        return self._form


def _adapter(username="fei"):
    adapter = MattermostAdapter(
        PlatformConfig(
            enabled=True,
            token=BOT_TOKEN,
            extra={"url": "https://mm.example.com"},
        )
    )
    adapter._bot_username = username
    adapter._bot_user_id = f"id-{username}"
    adapter._owner_profile = "default"
    adapter.handle_message = AsyncMock()
    return adapter


def _runner(router):
    return SimpleNamespace(
        adapters={Platform.MATTERMOST: router},
        _profile_adapters={},
        _active_profile_name=lambda: "default",
    )


@pytest.mark.asyncio
async def test_global_command_routes_status_to_default_profile(monkeypatch):
    monkeypatch.setenv("MATTERMOST_COMMAND_TOKEN", COMMAND_TOKEN)
    router = _adapter()
    router.gateway_runner = _runner(router)

    response = await router._handle_command_request(
        FakeRequest(
            {
                "text": "default status",
                "channel_id": "channel-1",
                "user_id": "user-1",
                "user_name": "russ",
                "trigger_id": "trigger-1",
            }
        )
    )
    await asyncio.sleep(0)

    assert response.status == 200
    router.handle_message.assert_awaited_once()
    event = router.handle_message.await_args.args[0]
    assert event.text == "/status"
    assert event.message_type is MessageType.COMMAND
    assert event.source.profile == "default"
    assert event.source.chat_id == "channel-1"
    assert event.source.user_id == "user-1"


@pytest.mark.asyncio
async def test_command_server_accepts_authenticated_http_request(monkeypatch):
    monkeypatch.setenv("MATTERMOST_COMMAND_TOKEN", COMMAND_TOKEN)
    monkeypatch.setenv("MATTERMOST_CALLBACK_HOST", "127.0.0.1")
    monkeypatch.setenv("MATTERMOST_CALLBACK_PORT", "0")
    router = _adapter()
    router.gateway_runner = _runner(router)

    await router._start_command_server()
    try:
        site = router._command_hub.site
        port = site._server.sockets[0].getsockname()[1]
        async with ClientSession() as session:
            response = await session.post(
                f"http://127.0.0.1:{port}/hermes-command",
                headers={"Authorization": f"Token {COMMAND_TOKEN}"},
                data={
                    "text": "default status",
                    "channel_id": "channel-1",
                    "user_id": "user-1",
                    "user_name": "russ",
                },
            )
            assert response.status == 200
        await asyncio.sleep(0)
        router.handle_message.assert_awaited_once()
    finally:
        await router._stop_command_server()


@pytest.mark.asyncio
async def test_connect_and_disconnect_manage_command_server(monkeypatch):
    monkeypatch.setenv("MATTERMOST_COMMAND_TOKEN", COMMAND_TOKEN)
    router = _adapter()
    router._api_get = AsyncMock(
        return_value={"id": "id-fei", "username": "fei"}
    )
    router._ws_loop = AsyncMock()
    router._start_command_server = AsyncMock()
    router._stop_command_server = AsyncMock()
    session = MagicMock(closed=False)
    session.close = AsyncMock()

    with patch("aiohttp.ClientSession", return_value=session):
        assert await router.connect() is True
        router._start_command_server.assert_awaited_once()
        await router.disconnect()

    router._stop_command_server.assert_awaited_once()


def test_yaml_callback_settings_seed_platform_extras(monkeypatch):
    for name in ("MATTERMOST_CALLBACK_HOST", "MATTERMOST_CALLBACK_PORT"):
        monkeypatch.delenv(name, raising=False)

    seeded = _apply_yaml_config(
        {},
        {"callback_host": "0.0.0.0", "callback_port": 18065},
    )

    assert seeded == {"callback_host": "0.0.0.0", "callback_port": 18065}
    adapter = MattermostAdapter(
        PlatformConfig(
            enabled=True,
            token=BOT_TOKEN,
            extra={"url": "https://mm.example.com", **seeded},
        )
    )
    assert adapter._command_host == "0.0.0.0"
    assert adapter._command_port == 18065
