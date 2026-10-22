import pytest
import json
from unittest.mock import patch, MagicMock


class TestParseChannelMappings:
    """Tests for CHANNEL_MAPPINGS parsing in config.py."""

    def _parse_with_env(self, env_overrides):
        """
        Helper: reload config module with the given environment overrides.
        Returns (CHANNEL_MAPPINGS, CHANNEL_MAP) from the freshly loaded config.
        
        We mock load_dotenv to prevent it from loading the real .env file,
        and use clear=True in patch.dict to start with a clean environment
        (then add back only the vars we want).
        """
        import importlib
        import sys

        # Remove cached config module so it re-executes
        if 'config' in sys.modules:
            del sys.modules['config']

        # We need some base env vars for the system to work (e.g. PATH),
        # but we want to control all TG_*/DISCORD_*/CHANNEL_MAPPINGS vars precisely.
        # Strategy: don't clear everything, but explicitly remove/set the relevant keys.
        relevant_keys = [
            'TG_API_ID', 'TG_API_HASH', 'TG_TARGET_CHANNEL_ID', 'TG_SESSION_STRING',
            'DISCORD_WEBHOOK_URL', 'CHANNEL_MAPPINGS',
            'GOOGLE_SHEET_ID', 'VERTEX_PROJECT_ID', 'VERTEX_LOCATION',
            'NOTION_API_KEY', 'NOTION_PAGE_ID',
            'SMTP_SERVER', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD',
            'EMAIL_FROM', 'EMAIL_TO',
            'AUTHORIZED_USER_JSON', 'VERTEX_CREDENTIALS_JSON',
        ]
        clean_env = {k: '' for k in relevant_keys}
        clean_env.update(env_overrides)

        with patch.dict('os.environ', clean_env, clear=False), \
             patch('sys.argv', ['test']), \
             patch('os.path.exists', return_value=False), \
             patch('dotenv.load_dotenv', return_value=None):
            import config
            return config.CHANNEL_MAPPINGS, config.CHANNEL_MAP

    def test_json_multiple_mappings(self):
        """CHANNEL_MAPPINGS with two entries should produce two ChannelMapping objects."""
        mappings_json = json.dumps([
            {"telegram_channel_id": -100111, "discord_webhook_url": "https://discord.com/webhook/1", "label": "chan-a"},
            {"telegram_channel_id": -100222, "discord_webhook_url": "https://discord.com/webhook/2", "label": "chan-b"},
        ])
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
            'TG_TARGET_CHANNEL_ID': '-100999',
            'DISCORD_WEBHOOK_URL': 'https://fallback',
            'CHANNEL_MAPPINGS': mappings_json,
        }
        mappings, channel_map = self._parse_with_env(env)

        assert len(mappings) == 2
        assert mappings[0].telegram_channel_id == -100111
        assert mappings[0].discord_webhook_url == "https://discord.com/webhook/1"
        assert mappings[0].label == "chan-a"
        assert mappings[1].telegram_channel_id == -100222
        assert mappings[1].label == "chan-b"

        # CHANNEL_MAP should allow O(1) lookup
        assert -100111 in channel_map
        assert -100222 in channel_map
        assert channel_map[-100111].label == "chan-a"

    def test_fallback_to_legacy_vars(self):
        """When CHANNEL_MAPPINGS is not set, should fall back to TG_TARGET_CHANNEL_ID + DISCORD_WEBHOOK_URL."""
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
            'TG_TARGET_CHANNEL_ID': '-100999',
            'DISCORD_WEBHOOK_URL': 'https://fallback-webhook',
        }
        mappings, channel_map = self._parse_with_env(env)

        assert len(mappings) == 1
        assert mappings[0].telegram_channel_id == -100999
        assert mappings[0].discord_webhook_url == "https://fallback-webhook"
        assert mappings[0].label == "default"
        assert -100999 in channel_map

    def test_empty_channel_mappings_falls_back(self):
        """Empty CHANNEL_MAPPINGS string should fall back to legacy vars."""
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
            'TG_TARGET_CHANNEL_ID': '-100999',
            'DISCORD_WEBHOOK_URL': 'https://fallback-webhook',
            'CHANNEL_MAPPINGS': '',
        }
        mappings, _ = self._parse_with_env(env)

        assert len(mappings) == 1
        assert mappings[0].telegram_channel_id == -100999

    def test_invalid_json_fails_closed(self):
        """Invalid explicit source config must stop startup rather than silently fall back."""
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
            'TG_TARGET_CHANNEL_ID': '-100999',
            'DISCORD_WEBHOOK_URL': 'https://fallback-webhook',
            'CHANNEL_MAPPINGS': 'not-valid-json{{{',
        }
        with pytest.raises(ValueError, match='CHANNEL_MAPPINGS is not valid JSON'):
            self._parse_with_env(env)

    def test_no_config_at_all_returns_empty(self):
        """No CHANNEL_MAPPINGS and no TG_TARGET_CHANNEL_ID should return empty list."""
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
        }
        mappings, channel_map = self._parse_with_env(env)

        assert len(mappings) == 0
        assert len(channel_map) == 0

    def test_label_defaults_to_channel_id(self):
        """If label is not provided, it should default to the telegram_channel_id string."""
        mappings_json = json.dumps([
            {"telegram_channel_id": -100333, "discord_webhook_url": "https://discord.com/webhook/3"},
        ])
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
            'CHANNEL_MAPPINGS': mappings_json,
        }
        mappings, _ = self._parse_with_env(env)

        assert len(mappings) == 1
        assert mappings[0].label == "-100333"

    def test_discord_webhook_optional(self):
        """discord_webhook_url should be optional (defaults to empty string)."""
        mappings_json = json.dumps([
            {"telegram_channel_id": -100444},
        ])
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
            'CHANNEL_MAPPINGS': mappings_json,
        }
        mappings, _ = self._parse_with_env(env)

        assert len(mappings) == 1
        assert mappings[0].discord_webhook_url == ""

    def test_empty_array_fails_closed(self):
        """An explicit empty mapping must be fixed rather than replaced with legacy settings."""
        env = {
            'TG_API_ID': '123', 'TG_API_HASH': 'abc',
            'TG_TARGET_CHANNEL_ID': '-100999',
            'DISCORD_WEBHOOK_URL': 'https://fallback-webhook',
            'CHANNEL_MAPPINGS': '[]',
        }
        with pytest.raises(ValueError, match='non-empty JSON array'):
            self._parse_with_env(env)


    def test_channel_mode_defaults_to_test(self):
        mappings_json = json.dumps([{"telegram_channel_id": -10042, "label": "safe-default", "enable_trading": True}])
        mappings, _ = self._parse_with_env({"CHANNEL_MAPPINGS": mappings_json})
        assert mappings[0].mode == "test"

    def test_channel_mode_can_be_explicitly_live(self):
        mappings_json = json.dumps([{"telegram_channel_id": -10043, "label": "live", "mode": "live", "enable_trading": True}])
        mappings, _ = self._parse_with_env({"CHANNEL_MAPPINGS": mappings_json})
        assert mappings[0].mode == "live"

    def test_string_false_does_not_enable_trading(self):
        mappings_json = json.dumps([{
            "telegram_channel_id": -10044,
            "label": "string-bools",
            "mode": "test",
            "enable_trading": "false",
            "enable_fno_trading": "0",
        }])
        mappings, _ = self._parse_with_env({"CHANNEL_MAPPINGS": mappings_json})
        assert mappings[0].enable_trading is False
        assert mappings[0].enable_fno_trading is False

    def test_invalid_boolean_config_fails_closed(self):
        mappings_json = json.dumps([{
            "telegram_channel_id": -10045,
            "enable_trading": "sometimes",
        }])
        with pytest.raises(ValueError, match='enable_trading must be a boolean'):
            self._parse_with_env({
                "CHANNEL_MAPPINGS": mappings_json,
                "TG_TARGET_CHANNEL_ID": "-100999",
            })
