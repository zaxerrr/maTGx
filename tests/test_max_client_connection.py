"""Tests for MaxClient connection health: auth failures, watchdog, backoff,
and the on_ready callback not blocking the WebSocket read loop."""

import asyncio
import json

from app.max_client import MaxClient, OpCode, _mask_token


class FakeWS:
    def __init__(self, client=None, reply_to_ping=True):
        self.closed = False
        self.sent: list[dict] = []
        self._client = client
        self._reply_to_ping = reply_to_ping

    async def send_str(self, raw: str) -> None:
        pkt = json.loads(raw)
        self.sent.append(pkt)
        if (self._client and self._reply_to_ping
                and pkt["opcode"] == OpCode.HEARTBEAT_PING):
            # Server echoes a cmd=1 reply with the same seq.
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self._client._handle(
                    {"cmd": 1, "seq": pkt["seq"], "opcode": 1, "payload": {}}
                ))
            )

    async def close(self) -> None:
        self.closed = True


def _client(**kw) -> MaxClient:
    c = MaxClient(token="secret-token", device_id="dev", **kw)
    return c


# ---------------------------------------------------------------------------
# Auth failures
# ---------------------------------------------------------------------------

class TestAuthFailure:
    async def test_auth_error_response_triggers_callback_and_closes_ws(self):
        c = _client()
        c._ws = FakeWS()
        reasons = []

        @c.on_auth_failed
        async def cb(reason):
            reasons.append(reason)

        await c._handle({"cmd": 3, "seq": 1, "opcode": OpCode.AUTH_SNAPSHOT,
                         "payload": {"error": "login.token"}})
        await asyncio.sleep(0)

        assert c._ws.closed
        assert len(reasons) == 1
        assert "login.token" in reasons[0]
        assert c._authorized is False

    async def test_handshake_error_is_also_reported(self):
        c = _client()
        c._ws = FakeWS()
        reasons = []
        c.on_auth_failed(lambda r: _append(reasons, r))

        await c._handle({"cmd": 3, "seq": 0, "opcode": OpCode.HANDSHAKE,
                         "payload": {"error": "proto.payload"}})
        await asyncio.sleep(0)

        assert c._ws.closed
        assert reasons and "handshake" in reasons[0]

    async def test_auth_success_sets_authorized(self):
        c = _client()
        await c._handle({"cmd": 1, "seq": 1, "opcode": OpCode.AUTH_SNAPSHOT,
                         "payload": {"profile": {"id": 7}}})
        assert c._authorized is True
        assert c._my_id == 7


async def _append(lst, item):
    lst.append(item)


# ---------------------------------------------------------------------------
# on_ready must not block the read loop
# ---------------------------------------------------------------------------

class TestOnReadyNonBlocking:
    async def test_rpc_inside_on_ready_gets_its_reply(self):
        c = _client()
        c._ws = FakeWS()
        results = []

        @c.on_ready
        async def ready(snapshot):
            results.append(await c.cmd(OpCode.CONTACT_GET, {"contactIds": [1]},
                                       timeout=2))

        # Emulate the serial read loop in run(): auth, then the contact reply.
        await c._handle({"cmd": 1, "seq": 99, "opcode": OpCode.AUTH_SNAPSHOT,
                         "payload": {"profile": {"id": 5}}})
        await asyncio.sleep(0)  # let on_ready send its request (seq 0)
        await c._handle({"cmd": 1, "seq": 0, "opcode": OpCode.CONTACT_GET,
                         "payload": {"contacts": [{"id": 1}]}})
        for _ in range(50):
            if results:
                break
            await asyncio.sleep(0.01)

        assert results == [{"contacts": [{"id": 1}]}]


# ---------------------------------------------------------------------------
# Watchdog (heartbeat loop)
# ---------------------------------------------------------------------------

class TestWatchdog:
    async def test_no_auth_snapshot_within_timeout_reports_and_closes(self):
        c = _client()
        c.HEARTBEAT_SEC = 0.01
        c.AUTH_TIMEOUT_SEC = 0.01
        ws = FakeWS(client=c)
        c._ws = ws
        reasons = []
        c.on_auth_failed(lambda r: _append(reasons, r))

        loop = asyncio.get_running_loop()
        await asyncio.wait_for(c._heartbeat_loop(ws, loop.time()), timeout=1)
        await asyncio.sleep(0)

        assert ws.closed
        assert reasons and "AUTH_SNAPSHOT" in reasons[0]

    async def test_unanswered_ping_closes_connection(self):
        c = _client()
        c.HEARTBEAT_SEC = 0.01
        c.PING_TIMEOUT_SEC = 0.02
        c._authorized = True
        ws = FakeWS(client=c, reply_to_ping=False)
        c._ws = ws

        loop = asyncio.get_running_loop()
        await asyncio.wait_for(c._heartbeat_loop(ws, loop.time()), timeout=1)

        assert ws.closed
        assert any(p["opcode"] == OpCode.HEARTBEAT_PING for p in ws.sent)

    async def test_answered_pings_keep_connection_open(self):
        c = _client()
        c.HEARTBEAT_SEC = 0.01
        c.PING_TIMEOUT_SEC = 0.5
        c._authorized = True
        ws = FakeWS(client=c, reply_to_ping=True)
        c._ws = ws

        loop = asyncio.get_running_loop()
        task = asyncio.create_task(c._heartbeat_loop(ws, loop.time()))
        await asyncio.sleep(0.1)
        assert not ws.closed
        assert sum(p["opcode"] == OpCode.HEARTBEAT_PING for p in ws.sent) >= 2
        task.cancel()


# ---------------------------------------------------------------------------
# Reconnect backoff
# ---------------------------------------------------------------------------

class TestReconnectBackoff:
    def test_backoff_doubles_while_unauthorized_and_caps(self):
        c = _client()
        c._authorized = False
        delays = [c._next_reconnect_delay() for _ in range(10)]
        assert delays[:4] == [5, 10, 20, 40]
        assert max(delays) == MaxClient.MAX_RECONNECT_SEC

    def test_successful_auth_resets_backoff(self):
        c = _client()
        for _ in range(4):
            c._next_reconnect_delay()
        c._authorized = True
        assert c._next_reconnect_delay() == MaxClient.RECONNECT_SEC

    def test_failed_connects_after_authorized_session_keep_backing_off(self):
        c = _client()
        c._authorized = True
        # Session ends, then connect attempts fail before any auth.
        delays = [c._next_reconnect_delay() for _ in range(4)]
        assert delays == [5, 10, 20, 40]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

class TestMisc:
    async def test_cmd_when_disconnected_returns_empty_immediately(self):
        c = _client()
        resp = await asyncio.wait_for(c.cmd(OpCode.CONTACT_GET, {}), timeout=0.5)
        assert resp == {}

    def test_chat_ids_not_shared_between_instances(self):
        a = MaxClient(token="t", device_id="d", chat_ids="1,2")
        b = MaxClient(token="t", device_id="d")
        assert a.chat_ids == [1, 2]
        assert b.chat_ids == []

    def test_mask_token_nested(self):
        pkt = {"opcode": 19, "payload": {"token": "abc", "chatsCount": 10,
                                         "list": [{"token": "x"}]}}
        masked = _mask_token(pkt)
        assert masked["payload"]["token"] == "***"
        assert masked["payload"]["list"][0]["token"] == "***"
        assert masked["payload"]["chatsCount"] == 10
        assert pkt["payload"]["token"] == "abc"  # original untouched

    async def test_debug_log_does_not_leak_token(self, caplog):
        import logging
        c = _client()
        c._ws = FakeWS()
        with caplog.at_level(logging.DEBUG, logger="app.max_client"):
            await c._send(OpCode.AUTH_SNAPSHOT, {"token": "secret-token"})
        assert "secret-token" not in caplog.text
        assert c._ws.sent[0]["payload"]["token"] == "secret-token"
