"""
Shared Dhan token: the equity client and the FnO agent must not invalidate each other's token.

Reproduces the live failure (equity order rejected with DH-906 "Invalid Token" after the FnO bot
logged in) against a fake Dhan that, like the real one, keeps only ONE valid token per account.
"""

import os
import sys
import types

import pytest

from client_agent import dhan_token


@pytest.fixture(autouse=True)
def token_file(tmp_path, monkeypatch):
    path = str(tmp_path / ".dhan_token")
    monkeypatch.setattr(dhan_token, "TOKEN_FILE", path)
    # pyotp is only needed to compute the TOTP code; the fake Dhan doesn't check it
    fake_pyotp = types.ModuleType("pyotp")
    fake_pyotp.TOTP = lambda secret: types.SimpleNamespace(now=lambda: "123456")
    monkeypatch.setitem(sys.modules, "pyotp", fake_pyotp)
    return path


# ---------------------------------------------------------------------------
# dhan_token helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status, body, expected", [
    (400, {"errorType": "Order_Error", "errorCode": "DH-906", "errorMessage": "Invalid Token"}, True),  # the live error
    (401, {}, True),
    (400, {"errorCode": "DH-901", "errorMessage": "Client ID or user generated access token is invalid or expired."}, True),
    (400, {"errorCode": "DH-906", "errorMessage": "Insufficient Funds"}, False),
    (200, {"orderId": "1"}, False),
])
def test_is_token_error(status, body, expected):
    assert dhan_token.is_token_error(status, body) is expected


def test_fresh_token_adopts_newer_shared_token_without_login(token_file):
    dhan_token._write_token("NEW", token_file)
    logins = []
    assert dhan_token.fresh_token("OLD", lambda: logins.append(1) or "X") == "NEW"
    assert logins == []


def test_fresh_token_logs_in_when_shared_token_is_the_rejected_one(token_file):
    dhan_token._write_token("OLD", token_file)
    assert dhan_token.fresh_token("OLD", lambda: "NEW") == "NEW"
    assert dhan_token.read_token() == "NEW"
    assert oct(os.stat(token_file).st_mode & 0o777) == "0o600"


def test_failed_login_keeps_file(token_file):
    dhan_token._write_token("OLD", token_file)
    assert dhan_token.fresh_token("OLD", lambda: None) is None
    assert dhan_token.read_token() == "OLD"


# ---------------------------------------------------------------------------
# Both bots against a fake Dhan with one-valid-token-per-account semantics
# ---------------------------------------------------------------------------

class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text = str(data)

    def json(self):
        return self._data


class FakeDhan:
    INVALID = {"errorType": "Order_Error", "errorCode": "DH-906", "errorMessage": "Invalid Token"}

    def __init__(self):
        self.valid, self.logins = "T0-expired", 0

    def _ok(self, headers):
        return (headers or {}).get("access-token") == self.valid

    def post(self, url, headers=None, json=None, params=None, timeout=None):
        if "generateAccessToken" in url:
            self.logins += 1
            self.valid = f"T{self.logins}"  # a new login invalidates the previous token
            return Resp(200, {"accessToken": self.valid})
        return Resp(200, {"orderId": "OK", "orderStatus": "PENDING"}) if self._ok(headers) else Resp(400, self.INVALID)

    def get(self, url, headers=None, timeout=None):  # /v2/profile
        return Resp(200, {"dhanClientId": "1"}) if self._ok(headers) else Resp(401, {"errorCode": "DH-901"})

    def request(self, method, url, headers=None, json=None, timeout=None):
        return Resp(200, {"orderId": "OK", "orderStatus": "PENDING"}) if self._ok(headers) else Resp(400, self.INVALID)


@pytest.fixture
def dhan(monkeypatch):
    import requests
    fake = FakeDhan()
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "request", fake.request)
    return fake


def _bots():
    from client_agent.client import DhanBroker
    from fno_agent.brokers.dhan import DhanFnOBroker

    equity = DhanBroker("1", "", pin="1234", totp_secret="JBSWY3DPEHPK3PXP", dry_run=False)
    equity._scrips.load = lambda: None  # no scrip-master download
    fno = DhanFnOBroker("1", "", pin="1234", totp_secret="JBSWY3DPEHPK3PXP", dry_run=False)
    return equity, fno


def test_bots_share_one_login_and_recover_from_the_others_refresh(dhan):
    equity, fno = _bots()

    equity.connect()  # first login ever
    fno.connect()  # adopts the shared token: NO second login that would kill the equity token
    assert dhan.logins == 1 and equity.access_token == fno.access_token == dhan.valid

    fno.refresh_session()  # FnO's daily 08:00 login -> equity's token is now invalid (the live failure)
    assert dhan.logins == 2 and equity.access_token != dhan.valid

    # Equity order: DH-906 -> adopts the FnO bot's new token from the file -> retry succeeds, no new login
    result = equity._place_dhan_order({"securityId": "1"})
    assert result == {"order_id": "OK", "status": "PENDING"}
    assert dhan.logins == 2 and equity.access_token == dhan.valid

    # FnO keeps working: equity didn't log in again and kill its token
    assert fno._request("POST", "/super/orders", {"x": 1})["orderId"] == "OK"
    assert dhan.logins == 2


def test_expired_shared_token_is_replaced_at_startup(dhan, token_file):
    dhan_token._write_token("T0-yesterday", token_file)  # older than 24h: Dhan rejects it
    equity, _ = _bots()
    equity.connect()
    assert dhan.logins == 1 and equity.access_token == dhan.valid == dhan_token.read_token()


def test_equity_retries_on_invalid_token_when_alone(dhan):
    equity, _ = _bots()
    equity.connect()
    dhan.valid = "rotated-elsewhere"  # e.g. a login from another device
    result = equity._place_dhan_order({"securityId": "1"})
    assert result["order_id"] == "OK" and dhan.logins == 2
