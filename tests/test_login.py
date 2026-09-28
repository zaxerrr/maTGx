"""Tests for app/login.py — phone/code login flow against a fake MAX server."""

import json
import os

import pytest
from aiohttp import web

from app import login
from app.login import LoginError, extract_login_token, normalize_phone, write_env


class TestHelpers:
    @pytest.mark.parametrize("raw", ["+7 999 123-45-67", "89991234567", "9991234567",
                                     "7 (999) 123 45 67"])
    def test_normalize_phone(self, raw):
        assert normalize_phone(raw) == "+79991234567"

    def test_normalize_phone_rejects_garbage(self):
        with pytest.raises(LoginError):
            normalize_phone("12")

    def test_extract_login_token(self):
        assert extract_login_token({"tokenAttrs": {"LOGIN": {"token": "abc"}}}) == "abc"
        assert extract_login_token({}) is None
        assert extract_login_token({"tokenAttrs": {"LOGIN": {}}}) is None

    def test_write_env_updates_and_appends(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("TG_BOT_TOKEN=x\nMAX_TOKEN=old\n# MAX_DEVICE_ID=\n", encoding="utf-8")
        write_env(str(p), {"MAX_TOKEN": "new", "MAX_DEVICE_ID": "dev", "EXTRA": "1"})
        assert p.read_text(encoding="utf-8").splitlines() == [
            "TG_BOT_TOKEN=x", "MAX_TOKEN=new", "MAX_DEVICE_ID=dev", "EXTRA=1"]
        assert oct(os.stat(p).st_mode & 0o777) == "0o600"


async def _fake_max(behaviour):
    """WS server answering like MAX; ``behaviour`` tweaks CHECK_CODE / AUTH."""
    seen = []

    async def handler(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        async for m in ws:
            p = json.loads(m.data)
            seen.append(p)
            op, pl = p["opcode"], p["payload"]
            cmd, out = 1, {}
            if op == 17:
                out = {"token": "sms-tok"}
            elif op == 18:
                if pl["verifyCode"] != behaviour.get("code", "123456"):
                    cmd, out = 3, {"error": "verify.code", "localizedMessage": "Неверный код"}
                else:
                    out = behaviour.get("check", {"tokenAttrs": {"LOGIN": {"token": "LOGIN-TOK"}}})
            elif op == 19:
                out = {"profile": {"id": 5, "names": [{"firstName": "Ann"}]}}
            await ws.send_str(json.dumps({"ver": 11, "cmd": cmd, "seq": p["seq"],
                                          "opcode": op, "payload": out}))
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws", seen


class TestFlow:
    async def _run(self, monkeypatch, tmp_path, behaviour, codes):
        runner, url, seen = await _fake_max(behaviour)
        monkeypatch.setattr(login.MaxClient, "WS_URL", url)
        it = iter(codes)
        monkeypatch.setattr(login.getpass, "getpass", lambda prompt="": next(it))
        env = tmp_path / ".env"
        try:
            rc = await login.run("+7 999 123 45 67", str(env), None)
        finally:
            await runner.cleanup()
        return rc, env, seen

    async def test_successful_login_writes_env(self, monkeypatch, tmp_path):
        rc, env, seen = await self._run(monkeypatch, tmp_path, {}, ["12-34-56"])
        assert rc == 0
        text = env.read_text(encoding="utf-8")
        assert "MAX_TOKEN=LOGIN-TOK" in text
        device = [l.split("=", 1)[1] for l in text.splitlines() if l.startswith("MAX_DEVICE_ID=")][0]
        # The same deviceId is used for the code request and the token check.
        handshakes = [p["payload"]["deviceId"] for p in seen if p["opcode"] == 6]
        assert handshakes == [device, device]
        start = [p for p in seen if p["opcode"] == 17][0]["payload"]
        assert start["phone"] == "+79991234567"
        auth = [p for p in seen if p["opcode"] == 19][0]["payload"]
        assert auth["token"] == "LOGIN-TOK"

    async def test_wrong_code_then_right_code(self, monkeypatch, tmp_path):
        rc, env, seen = await self._run(monkeypatch, tmp_path, {}, ["000000", "123456"])
        assert rc == 0
        assert [p["payload"]["verifyCode"] for p in seen if p["opcode"] == 18] == ["000000", "123456"]

    async def test_three_wrong_codes_fail(self, monkeypatch, tmp_path):
        with pytest.raises(LoginError, match="Неверный код"):
            await self._run(monkeypatch, tmp_path, {}, ["1", "2", "3"])

    async def test_missing_login_token_is_explained(self, monkeypatch, tmp_path):
        with pytest.raises(LoginError, match="облачный пароль"):
            await self._run(monkeypatch, tmp_path, {"check": {"passwordChallenge": {}}}, ["123456"])
