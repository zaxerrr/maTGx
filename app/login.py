"""Log in to MAX by phone number + confirmation code and print a bridge token.

Use it when MAX_TOKEN from web.max.ru is missing, rejected or rotated:

    python -m app.login                     # asks for phone and code
    python -m app.login --phone +79991234567 --write-env .env
    docker compose run --rm max2tg python -m app.login   # on the server

Protocol (see nsdkinx/vkmax): HANDSHAKE (op 6) with a fresh deviceId →
op 17 START_AUTH {phone} → code arrives by SMS or in the MAX app →
op 18 CHECK_CODE {token, verifyCode} → payload.tokenAttrs.LOGIN.token.
The token is then verified with a real AUTH_SNAPSHOT (op 19).

The deviceId generated here must be used together with the token, so both
MAX_TOKEN and MAX_DEVICE_ID are printed / written.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import uuid

import aiohttp

from app.max_client import _BROWSER_HEADERS, _USER_AGENT, _WS_HEADERS, MaxClient, OpCode

START_AUTH = 17
CHECK_CODE = 18


class LoginError(Exception):
    pass


class CodeRejected(LoginError):
    """MAX rejected the confirmation code — the user may retry."""


class _Rpc:
    """Minimal request/response client over one MAX WebSocket."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse):
        self._ws = ws
        self._seq = 0
        self._lock = asyncio.Lock()   # one reader at a time (keepalive vs. calls)

    async def call(self, opcode: int, payload: dict, timeout: float = 20) -> dict:
        async with self._lock:
            return await self._call(opcode, payload, timeout)

    async def _call(self, opcode: int, payload: dict, timeout: float) -> dict:
        seq = self._seq
        self._seq += 1
        await self._ws.send_str(json.dumps(
            {"ver": 11, "cmd": 0, "seq": seq, "opcode": opcode, "payload": payload},
            ensure_ascii=False,
        ))
        try:
            # wait_for, not asyncio.timeout: keeps Python 3.10 (Ubuntu 22.04) working.
            return await asyncio.wait_for(self._reply(seq), timeout)
        except asyncio.TimeoutError:
            raise LoginError(f"MAX не ответил за {timeout:.0f} с (op {opcode})") from None

    async def _reply(self, seq: int) -> dict:
        while True:
            msg = await self._ws.receive()
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise LoginError(f"соединение с MAX закрыто ({msg.type.name})")
            data = json.loads(msg.data)
            if data.get("seq") != seq or data.get("cmd") not in (1, 3):
                continue  # server events, pings, ...
            payload = data.get("payload") or {}
            if data["cmd"] == 3 or "error" in payload:
                raise LoginError(_describe(payload))
            return payload


def _describe(err: dict) -> str:
    return (err.get("localizedMessage") or err.get("message")
            or err.get("error") or json.dumps(err, ensure_ascii=False))


def _handshake_payload(device_id: str) -> dict:
    return {
        "deviceId": device_id,
        "userAgent": {
            "deviceType": "WEB",
            "locale": "ru",
            "deviceLocale": "ru",
            "osVersion": "Linux",
            "deviceName": "Chrome",
            "headerUserAgent": _USER_AGENT,
            "appVersion": "26.2.2",
            "screen": "1080x1920 1.0x",
            "timezone": "Europe/Moscow",
        },
    }


def normalize_phone(raw: str) -> str:
    """'8 (999) 123-45-67' / '+7 999 1234567' → '+79991234567'."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 10 <= len(digits) <= 15:
        raise LoginError(f"не похоже на номер телефона: {raw!r}")
    return "+" + digits


def extract_login_token(payload: dict) -> str | None:
    token = ((payload.get("tokenAttrs") or {}).get("LOGIN") or {}).get("token")
    return token if isinstance(token, str) and token else None


async def request_code(rpc: _Rpc, device_id: str, phone: str) -> str:
    await rpc.call(OpCode.HANDSHAKE, _handshake_payload(device_id))
    resp = await rpc.call(START_AUTH, {"phone": phone, "type": "START_AUTH", "language": "ru"})
    sms_token = resp.get("token")
    if not sms_token:
        raise LoginError(f"MAX не выдал токен подтверждения: {json.dumps(resp, ensure_ascii=False)[:300]}")
    return sms_token


async def check_code(rpc: _Rpc, sms_token: str, code: str) -> tuple[str, dict]:
    try:
        resp = await rpc.call(CHECK_CODE, {
            "token": sms_token, "verifyCode": code, "authTokenType": "CHECK_CODE",
        })
    except LoginError as e:
        raise CodeRejected(str(e)) from e
    token = extract_login_token(resp)
    if not token:
        # e.g. an account with a cloud password (2FA) — not supported yet.
        raise LoginError(
            "код принят, но токен входа не пришёл (возможно, на аккаунте включён "
            f"облачный пароль). Ключи ответа: {sorted(resp)}"
        )
    return token, resp


async def verify_token(session: aiohttp.ClientSession, token: str, device_id: str,
                       proxy: str | None) -> dict:
    """Log in with the token exactly as the bridge does; return the profile."""
    async with session.ws_connect(MaxClient.WS_URL, headers=_WS_HEADERS, proxy=proxy) as ws:
        rpc = _Rpc(ws)
        await rpc.call(OpCode.HANDSHAKE, _handshake_payload(device_id))
        snap = await rpc.call(OpCode.AUTH_SNAPSHOT, {
            "interactive": True, "token": token, "chatsCount": 1,
        })
        return snap.get("profile") or {}


async def check_token(token: str, device_id: str, proxy: str | None = None) -> dict:
    """Validate a MAX token + device id with a real login; return the profile.
    Raises LoginError if MAX rejects it."""
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        return await verify_token(session, token, device_id, proxy)


class PhoneLogin:
    """Two-step phone login that keeps the MAX connection open between
    "send me a code" and "here is the code" — for the bot's chat dialog.

        login = PhoneLogin(proxy)
        await login.start("+7999...")      # MAX sends the code
        token, device_id, profile = await login.finish("123456")
        await login.close()

    ``finish`` raises ``CodeRejected`` for a wrong code (call it again) and
    ``LoginError`` for anything final.
    """

    KEEPALIVE_SEC = 25

    def __init__(self, proxy: str | None = None):
        self.proxy = proxy
        self.device_id = str(uuid.uuid4())
        self.phone = ""
        self._session: aiohttp.ClientSession | None = None
        self._ws = None
        self._rpc: _Rpc | None = None
        self._sms_token = ""
        self._keepalive: asyncio.Task | None = None

    async def start(self, phone: str) -> str:
        self.phone = normalize_phone(phone)
        self._session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
        try:
            self._ws = await self._session.ws_connect(
                MaxClient.WS_URL, headers=_WS_HEADERS, proxy=self.proxy)
            self._rpc = _Rpc(self._ws)
            self._sms_token = await request_code(self._rpc, self.device_id, self.phone)
        except Exception:
            await self.close()
            raise
        self._keepalive = asyncio.create_task(self._ping_loop())
        return self.phone

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(self.KEEPALIVE_SEC)
            try:
                await self._rpc.call(OpCode.HEARTBEAT_PING, {"interactive": False})
            except Exception:
                return

    async def finish(self, code: str) -> tuple[str, str, dict]:
        if not self._rpc:
            raise LoginError("вход не начат")
        code = re.sub(r"\D", "", code or "")
        if not code:
            raise CodeRejected("код должен состоять из цифр")
        token, _ = await check_code(self._rpc, self._sms_token, code)
        await self.close()
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            profile = await verify_token(session, token, self.device_id, self.proxy)
        return token, self.device_id, profile

    async def close(self) -> None:
        if self._keepalive:
            self._keepalive.cancel()
            self._keepalive = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()


def profile_name(profile: dict) -> str:
    return _profile_name(profile)


def write_env(path: str, values: dict) -> None:
    """Set KEY=value lines in a dotenv file, keeping everything else."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    for key, value in values.items():
        pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                break
        else:
            lines.append(f"{key}={value}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _profile_name(profile: dict) -> str:
    names = profile.get("names") or []
    if names and isinstance(names[0], dict):
        n = names[0]
        full = f"{n.get('firstName', '')} {n.get('lastName', '')}".strip()
        if full or n.get("name"):
            return full or n["name"]
    return str(profile.get("id", "?"))


async def run(phone: str | None, write_env_path: str | None, proxy: str | None) -> int:
    phone = normalize_phone(phone or input("Номер телефона аккаунта MAX (+7…): "))
    device_id = str(uuid.uuid4())

    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        async with session.ws_connect(MaxClient.WS_URL, headers=_WS_HEADERS,
                                      proxy=proxy) as ws:
            rpc = _Rpc(ws)
            sms_token = await request_code(rpc, device_id, phone)
            print(f"Код отправлен на {phone} (SMS или в приложение MAX).")
            for attempt in range(3):
                code = re.sub(r"\D", "", getpass.getpass("Код подтверждения: "))
                try:
                    token, _ = await check_code(rpc, sms_token, code)
                    break
                except CodeRejected as e:
                    if attempt == 2:
                        raise
                    print(f"Не подошло: {e}. Попробуйте ещё раз.")

        profile = await verify_token(session, token, device_id, proxy)

    print(f"\nВход выполнен: {_profile_name(profile)} (id {profile.get('id', '?')}). "
          "Токен проверен.")
    values = {"MAX_TOKEN": token, "MAX_DEVICE_ID": device_id}
    if write_env_path:
        write_env(write_env_path, values)
        print(f"MAX_TOKEN и MAX_DEVICE_ID записаны в {write_env_path} (права 600).")
    else:
        print("\nДобавьте в .env / переменные окружения (никому не показывайте):\n")
        for k, v in values.items():
            print(f"{k}={v}")
    print("\nНе входите потом в web.max.ru с этими данными — токен может смениться.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.login",
        description="Войти в MAX по номеру и коду и получить MAX_TOKEN/MAX_DEVICE_ID.",
    )
    parser.add_argument("--phone", help="номер аккаунта, например +79991234567")
    parser.add_argument("--write-env", metavar="PATH",
                        help="записать значения в dotenv-файл (например .env)")
    parser.add_argument("--proxy", default=os.environ.get("MAX_PROXY") or None,
                        help="HTTP(S)-прокси для MAX (по умолчанию MAX_PROXY)")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args.phone, args.write_env, args.proxy))
    except LoginError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
