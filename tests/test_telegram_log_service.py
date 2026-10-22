from types import SimpleNamespace

from services import telegram_log_service as telegram_logs


def test_test_and_live_logs_use_separate_chat_ids(monkeypatch):
    posted = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def fake_post(url, json, timeout):
        posted.append((url, json["chat_id"], json["text"], timeout))
        return FakeResponse()

    monkeypatch.setenv("TELEGRAM_LOG_BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("TELEGRAM_TEST_LOG_CHAT_ID", "-100111")
    monkeypatch.setenv("TELEGRAM_LIVE_LOG_CHAT_ID", "-100222")
    monkeypatch.setattr(telegram_logs.requests, "post", fake_post)

    assert telegram_logs.send_telegram_log_sync("test message", mode="test") is True
    assert telegram_logs.send_telegram_log_sync("live message", mode="live") is True
    assert [row[1] for row in posted] == ["-100111", "-100222"]
    assert [row[2] for row in posted] == ["test message", "live message"]


def test_test_log_does_not_fall_back_to_live_chat(monkeypatch):
    posted = []

    monkeypatch.setenv("TELEGRAM_LOG_BOT_TOKEN", "123:test-token")
    monkeypatch.delenv("TELEGRAM_TEST_LOG_CHAT_ID", raising=False)
    monkeypatch.delenv("TEST_LOG_GROUP_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_LIVE_LOG_CHAT_ID", "-100222")
    monkeypatch.setattr(telegram_logs.requests, "post", lambda *a, **k: posted.append((a, k)))

    assert telegram_logs.send_telegram_log_sync("test message", mode="test") is False
    assert posted == []


def test_invalid_log_mode_fails_closed_without_using_live_destination(monkeypatch):
    monkeypatch.setenv("TELEGRAM_LOG_BOT_TOKEN", "123:test-token")
    monkeypatch.setenv("TELEGRAM_TEST_LOG_CHAT_ID", "-100111")
    monkeypatch.setenv("TELEGRAM_LIVE_LOG_CHAT_ID", "-100222")

    token, destination = telegram_logs.telegram_log_credentials("lve")

    assert token == "123:test-token"
    assert destination == ""
