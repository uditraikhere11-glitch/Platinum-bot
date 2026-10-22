import json

import pytest

import config


def test_channel_modes_default_to_test_and_string_false_is_false(monkeypatch):
    monkeypatch.setenv(
        "CHANNEL_MAPPINGS",
        json.dumps([
            {
                "telegram_channel_id": -100111,
                "label": "test-source",
                "enable_trading": "false",
                "enable_fno_trading": False,
            },
            {
                "telegram_channel_id": -100222,
                "label": "live-source",
                "mode": "live",
                "enable_trading": True,
            },
        ]),
    )

    mappings = config._parse_channel_mappings()

    assert [(m.label, m.mode) for m in mappings] == [
        ("test-source", "test"),
        ("live-source", "live"),
    ]
    assert mappings[0].enable_trading is False
    assert mappings[1].enable_trading is True


def test_duplicate_source_channel_fails_closed(monkeypatch):
    monkeypatch.setenv(
        "CHANNEL_MAPPINGS",
        json.dumps([
            {"telegram_channel_id": -100111, "label": "first", "mode": "test"},
            {"telegram_channel_id": -100111, "label": "second", "mode": "live"},
        ]),
    )

    with pytest.raises(ValueError, match="Duplicate telegram_channel_id"):
        config._parse_channel_mappings()


def test_invalid_mode_fails_closed(monkeypatch):
    monkeypatch.setenv(
        "CHANNEL_MAPPINGS",
        json.dumps([{"telegram_channel_id": -100111, "label": "bad", "mode": "production"}]),
    )

    with pytest.raises(ValueError, match="channel mode must be 'test' or 'live'"):
        config._parse_channel_mappings()


def test_duplicate_labels_fail_closed(monkeypatch):
    monkeypatch.setenv(
        "CHANNEL_MAPPINGS",
        json.dumps([
            {"telegram_channel_id": -100111, "label": "premium", "mode": "test"},
            {"telegram_channel_id": -100222, "label": "PREMIUM", "mode": "live"},
        ]),
    )

    with pytest.raises(ValueError, match="Duplicate channel label"):
        config._parse_channel_mappings()
