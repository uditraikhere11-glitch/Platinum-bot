from fno_agent.settings import Settings


def test_fno_dry_run_uses_test_log_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_LOG_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TELEGRAM_TEST_LOG_CHAT_ID", "-100111")
    monkeypatch.setenv("TELEGRAM_LIVE_LOG_CHAT_ID", "-100222")
    monkeypatch.setenv("DRY_RUN", "true")

    settings = Settings.from_env()

    assert settings.tg_bot_token == "123:test"
    assert settings.tg_chat_id == "-100111"
    assert settings.dry_run is True


def test_fno_live_mode_uses_live_log_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_LOG_BOT_TOKEN", "123:test")
    monkeypatch.setenv("TELEGRAM_TEST_LOG_CHAT_ID", "-100111")
    monkeypatch.setenv("TELEGRAM_LIVE_LOG_CHAT_ID", "-100222")
    monkeypatch.setenv("DRY_RUN", "false")

    settings = Settings.from_env()

    assert settings.tg_bot_token == "123:test"
    assert settings.tg_chat_id == "-100222"
    assert settings.dry_run is False
