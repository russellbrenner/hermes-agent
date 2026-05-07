"""Tests for the OAuth-passthrough escape hatch in build_anthropic_client.

Covers the 8-cell auth matrix:

  URL                         | token       | passthrough | expected
  ----------------------------+-------------+-------------+--------------------
  api.anthropic.com           | api-key     | n/a         | api_key=, x-api-key
  api.anthropic.com           | OAuth       | n/a         | auth_token=, Bearer + identity
  litellm/v1/anthropic        | api-key     | False       | api_key=, x-api-key
  litellm/v1/anthropic        | api-key     | True        | api_key=, x-api-key (flag is OAuth-only)
  litellm/v1/anthropic        | OAuth       | False       | api_key=, x-api-key (current behaviour preserved)
  litellm/v1/anthropic        | OAuth       | True        | auth_token=, Bearer + identity   ← THE FIX
  foo.azure.com               | OAuth       | True        | api_key=, x-api-key (Azure carve-out)
  bedrock-runtime.us-east-1.. | OAuth       | True        | api_key=, x-api-key (Bedrock carve-out)

Plus tests for the config-driven beta header / claude_code_version / user_agent
overrides reachable via ``anthropic.protocol.*``.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.anthropic_adapter import (
    _forces_x_api_key_auth,
    _resolve_claude_code_user_agent,
    _resolve_common_betas,
    _resolve_oauth_only_betas,
    build_anthropic_client,
    resolve_passthrough_llm_headers,
)


# Token fixtures — match the shape detection in _is_oauth_token.
_API_KEY = "sk-ant-api03-" + "x" * 60
_OAUTH = "sk-ant-oat01-" + "x" * 60


# ----------------------------------------------------------------------------
# 8-cell auth matrix
# ----------------------------------------------------------------------------

class TestAuthMatrix:
    """Verify the auth-construction branch picked for each (URL, token, flag) cell."""

    @staticmethod
    def _build(api_key, base_url=None, passthrough=False):
        with patch("agent.anthropic_adapter._anthropic_sdk") as mock_sdk:
            mock_sdk.Anthropic = MagicMock(return_value="client")
            build_anthropic_client(
                api_key, base_url=base_url, passthrough_oauth=passthrough,
            )
            assert mock_sdk.Anthropic.call_count == 1
            return mock_sdk.Anthropic.call_args.kwargs

    def test_native_anthropic_api_key_uses_x_api_key(self):
        kwargs = self._build(_API_KEY)
        assert "api_key" in kwargs and "auth_token" not in kwargs
        assert kwargs["api_key"] == _API_KEY

    def test_native_anthropic_oauth_uses_bearer_with_identity(self):
        kwargs = self._build(_OAUTH)
        assert "auth_token" in kwargs and "api_key" not in kwargs
        assert kwargs["auth_token"] == _OAUTH
        headers = kwargs["default_headers"]
        assert "claude-code-20250219" in headers["anthropic-beta"]
        assert "oauth-2025-04-20" in headers["anthropic-beta"]
        assert headers["user-agent"].startswith("claude-cli/")
        assert headers["x-app"] == "cli"

    def test_litellm_api_key_passthrough_off(self):
        kwargs = self._build(
            _API_KEY, base_url="https://litellm.example.com/v1/anthropic",
            passthrough=False,
        )
        assert kwargs.get("api_key") == _API_KEY
        assert "auth_token" not in kwargs

    def test_litellm_api_key_passthrough_on_unaffected(self):
        # Flag is OAuth-only — non-OAuth tokens stay on x-api-key.
        kwargs = self._build(
            _API_KEY, base_url="https://litellm.example.com/v1/anthropic",
            passthrough=True,
        )
        assert kwargs.get("api_key") == _API_KEY
        assert "auth_token" not in kwargs

    def test_litellm_oauth_passthrough_off_preserves_current_behaviour(self):
        # This is the pre-patch "broken" behaviour — kept as default for safety.
        kwargs = self._build(
            _OAUTH, base_url="https://litellm.example.com/v1/anthropic",
            passthrough=False,
        )
        assert kwargs.get("api_key") == _OAUTH
        assert "auth_token" not in kwargs

    def test_litellm_oauth_passthrough_on_emits_oauth_headers(self):
        # The fix: OAuth identity flows through the proxy when opt-in is set.
        kwargs = self._build(
            _OAUTH, base_url="https://litellm.example.com/v1/anthropic",
            passthrough=True,
        )
        assert kwargs.get("auth_token") == _OAUTH
        assert "api_key" not in kwargs
        headers = kwargs["default_headers"]
        assert "claude-code-20250219" in headers["anthropic-beta"]
        assert "oauth-2025-04-20" in headers["anthropic-beta"]
        assert headers["user-agent"].startswith("claude-cli/")
        assert headers["x-app"] == "cli"

    def test_azure_oauth_passthrough_blocked(self):
        # Azure cannot accept OAuth Bearer — carve-out wins over flag.
        kwargs = self._build(
            _OAUTH, base_url="https://my-deploy.azure.com/anthropic",
            passthrough=True,
        )
        assert kwargs.get("api_key") == _OAUTH
        assert "auth_token" not in kwargs

    def test_bedrock_oauth_passthrough_blocked(self):
        kwargs = self._build(
            _OAUTH,
            base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
            passthrough=True,
        )
        assert kwargs.get("api_key") == _OAUTH
        assert "auth_token" not in kwargs


# ----------------------------------------------------------------------------
# Carve-out helper
# ----------------------------------------------------------------------------

class TestForcesXApiKeyAuth:
    @pytest.mark.parametrize("url,expected", [
        ("https://my.azure.com/foo", True),
        ("https://bedrock-runtime.us-east-1.amazonaws.com", True),
        ("https://litellm.example.com/v1/anthropic", False),
        ("https://api.anthropic.com", False),
        ("", False),
        (None, False),
    ])
    def test_carveout(self, url, expected):
        assert _forces_x_api_key_auth(url) is expected


# ----------------------------------------------------------------------------
# Config-driven overrides
# ----------------------------------------------------------------------------

class TestConfigOverrides:
    @staticmethod
    def _patch_cfg(proto):
        return patch(
            "agent.anthropic_adapter._load_anthropic_protocol_config",
            return_value=proto,
        )

    def test_extend_betas_appends_to_common(self):
        with self._patch_cfg({"extend_betas": ["custom-beta-2026-05-01"]}):
            betas = _resolve_common_betas()
        assert "custom-beta-2026-05-01" in betas
        # Default betas still present
        assert "interleaved-thinking-2025-05-14" in betas

    def test_common_betas_override_replaces_defaults(self):
        with self._patch_cfg({"common_betas": ["only-this-2030"]}):
            betas = _resolve_common_betas()
        assert betas == ["only-this-2030"]

    def test_oauth_only_betas_override(self):
        with self._patch_cfg({"oauth_only_betas": ["custom-oauth-beta"]}):
            betas = _resolve_oauth_only_betas()
        assert betas == ["custom-oauth-beta"]

    def test_claude_code_version_override(self):
        with self._patch_cfg({"claude_code_version": "9.9.9"}):
            ua = _resolve_claude_code_user_agent()
        assert "claude-cli/9.9.9" in ua

    def test_user_agent_template_override(self):
        proto = {
            "claude_code_version": "1.0.0",
            "user_agent": "custom/{version}",
        }
        with self._patch_cfg(proto):
            ua = _resolve_claude_code_user_agent()
        assert ua == "custom/1.0.0"

    def test_user_agent_template_bad_format_falls_back(self):
        # Template missing {version} placeholder — falls back to default template.
        with self._patch_cfg({"user_agent": "no_placeholder"}):
            ua = _resolve_claude_code_user_agent()
        assert "claude-cli/" in ua

    def test_no_config_returns_defaults(self):
        with self._patch_cfg({}):
            common = _resolve_common_betas()
            oauth = _resolve_oauth_only_betas()
        assert "interleaved-thinking-2025-05-14" in common
        assert "claude-code-20250219" in oauth


# ----------------------------------------------------------------------------
# resolve_passthrough_llm_headers: per-provider vs model-section, narrowest wins
# ----------------------------------------------------------------------------

class TestResolvePassthroughLlmHeaders:
    @staticmethod
    def _patch_load_config(cfg):
        return patch("hermes_cli.config.load_config", return_value=cfg)

    def test_default_false_when_no_config(self):
        with self._patch_load_config({}):
            assert resolve_passthrough_llm_headers() is False

    def test_model_section_true(self):
        with self._patch_load_config({"model": {"passthrough_llm_headers": True}}):
            assert resolve_passthrough_llm_headers() is True

    def test_provider_entry_true_overrides_model_false(self):
        cfg = {
            "model": {"passthrough_llm_headers": False},
            "providers": {"my-litellm": {"passthrough_llm_headers": True}},
        }
        with self._patch_load_config(cfg):
            assert resolve_passthrough_llm_headers("my-litellm") is True
            # Other providers fall back to model.passthrough_llm_headers
            assert resolve_passthrough_llm_headers("anthropic") is False

    def test_provider_entry_false_overrides_model_true(self):
        cfg = {
            "model": {"passthrough_llm_headers": True},
            "providers": {"strict-byok": {"passthrough_llm_headers": False}},
        }
        with self._patch_load_config(cfg):
            assert resolve_passthrough_llm_headers("strict-byok") is False
            assert resolve_passthrough_llm_headers() is True

    def test_no_provider_arg_uses_model_section(self):
        cfg = {
            "model": {"passthrough_llm_headers": True},
            "providers": {"anthropic": {"passthrough_llm_headers": False}},
        }
        with self._patch_load_config(cfg):
            # No provider name passed — model-section wins.
            assert resolve_passthrough_llm_headers() is True

    def test_malformed_config_returns_false(self):
        # Config load raises — defensive default kicks in.
        with patch("hermes_cli.config.load_config", side_effect=Exception("boom")):
            assert resolve_passthrough_llm_headers() is False
