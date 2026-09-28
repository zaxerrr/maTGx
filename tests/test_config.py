"""Tests for app/config.py — load_settings."""

import os
import pytest
from unittest.mock import patch

from app.config import Settings, load_settings


def _load_settings_with_env(env: dict) -> Settings:
    """Call load_settings with a fully isolated environment, suppressing .env file loading."""
    with patch("app.config.load_dotenv"), patch.dict(os.environ, env, clear=True):
        return load_settings()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_ENV = {
    "MAX_TOKEN": "token123",
    "MAX_DEVICE_ID": "device-abc",
    "TG_BOT_TOKEN": "123456:AAABBBCCC",
    "TG_CHAT_ID": "-100123456",
}


def _env(**overrides):
    """Return a copy of the valid env dict with any overrides applied."""
    e = dict(_VALID_ENV)
    e.update(overrides)
    return e


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------

class TestSettingsDataclass:
    def test_frozen(self):
        s = Settings(
            max_token="t", max_device_id="d", tg_bot_token="b", tg_chat_id="c"
        )
        with pytest.raises((AttributeError, TypeError)):
            s.max_token = "changed"  # type: ignore[misc]

    def test_defaults(self):
        s = Settings(tg_bot_token="b")
        assert s.debug is False
        assert s.reply_enabled is True
        assert s.max_chat_ids is None
        assert s.max_token is None and s.tg_chat_id is None


# ---------------------------------------------------------------------------
# load_settings — valid env
# ---------------------------------------------------------------------------

class TestLoadSettingsValid:
    def test_required_fields_populated(self):
        s = _load_settings_with_env(_env())
        assert s.max_token == "token123"
        assert s.max_device_id == "device-abc"
        assert s.tg_bot_token == "123456:AAABBBCCC"
        assert s.tg_chat_id == "-100123456"

    def test_debug_default_false(self):
        s = _load_settings_with_env(_env())
        assert s.debug is False

    def test_debug_true_via_1(self):
        s = _load_settings_with_env(_env(DEBUG="1"))
        assert s.debug is True

    def test_debug_true_via_true(self):
        s = _load_settings_with_env(_env(DEBUG="true"))
        assert s.debug is True

    def test_debug_true_via_yes(self):
        s = _load_settings_with_env(_env(DEBUG="yes"))
        assert s.debug is True

    def test_debug_true_mixed_case(self):
        s = _load_settings_with_env(_env(DEBUG="True"))
        assert s.debug is True

    def test_debug_false_via_empty_string(self):
        s = _load_settings_with_env(_env(DEBUG=""))
        assert s.debug is False

    def test_debug_false_via_0(self):
        s = _load_settings_with_env(_env(DEBUG="0"))
        assert s.debug is False

    def test_debug_false_via_no(self):
        s = _load_settings_with_env(_env(DEBUG="no"))
        assert s.debug is False

    def test_reply_enabled_default_true(self):
        s = _load_settings_with_env(_env())
        assert s.reply_enabled is True

    def test_reply_enabled_true_via_1(self):
        s = _load_settings_with_env(_env(REPLY_ENABLED="1"))
        assert s.reply_enabled is True

    def test_reply_enabled_true_via_yes(self):
        s = _load_settings_with_env(_env(REPLY_ENABLED="yes"))
        assert s.reply_enabled is True

    def test_reply_enabled_false_via_false(self):
        s = _load_settings_with_env(_env(REPLY_ENABLED="false"))
        assert s.reply_enabled is False

    def test_returns_settings_instance(self):
        s = _load_settings_with_env(_env())
        assert isinstance(s, Settings)

    def test_max_chat_ids_none_when_not_set(self):
        s = _load_settings_with_env(_env())
        assert s.max_chat_ids is None

    def test_max_chat_ids_populated_when_set(self):
        s = _load_settings_with_env(_env(MAX_CHAT_IDS="-123,-456"))
        assert s.max_chat_ids == "-123,-456"

    def test_max_chat_ids_none_when_empty_string(self):
        s = _load_settings_with_env(_env(MAX_CHAT_IDS=""))
        assert s.max_chat_ids is None


# ---------------------------------------------------------------------------
# load_settings — missing required variables
# ---------------------------------------------------------------------------

class TestLoadSettingsMinimal:
    """Only TG_BOT_TOKEN is required; MAX accounts are set up in the bot chat."""

    def test_only_bot_token(self):
        s = _load_settings_with_env({"TG_BOT_TOKEN": "1:x"})
        assert s.tg_bot_token == "1:x"
        assert s.max_token is None and s.max_device_id is None
        assert s.tg_chat_id is None and s.tg_allowed_user_id is None

    def test_missing_tg_bot_token_raises(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env({})
        assert "TG_BOT_TOKEN" in str(exc.value)

    def test_empty_bot_token_raises(self):
        with pytest.raises(SystemExit):
            _load_settings_with_env({"TG_BOT_TOKEN": ""})

    def test_max_token_without_device_id_raises(self):
        with pytest.raises(SystemExit) as exc:
            _load_settings_with_env({"TG_BOT_TOKEN": "1:x", "MAX_TOKEN": "t"})
        assert "MAX_DEVICE_ID" in str(exc.value)

    def test_invalid_chat_id_raises(self):
        with pytest.raises(SystemExit):
            _load_settings_with_env({"TG_BOT_TOKEN": "1:x", "TG_CHAT_ID": "abc"})

    def test_invalid_allowed_user_raises(self):
        with pytest.raises(SystemExit):
            _load_settings_with_env({"TG_BOT_TOKEN": "1:x", "TG_ALLOWED_USER_ID": "me"})

    def test_allowed_user_parsed(self):
        s = _load_settings_with_env({"TG_BOT_TOKEN": "1:x", "TG_ALLOWED_USER_ID": "42"})
        assert s.tg_allowed_user_id == 42


def test_max_proxy_optional():
    assert _load_settings_with_env(_env()).max_proxy is None
    s = _load_settings_with_env(_env(MAX_PROXY="http://u:p@h:3128"))
    assert s.max_proxy == "http://u:p@h:3128"
