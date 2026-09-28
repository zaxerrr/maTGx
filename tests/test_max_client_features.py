"""Tests for MaxClient message/media RPCs: reply, edit, delete, video/file
download URLs, upload readiness and size-capped downloads."""

import asyncio
import json

from aiohttp import web

from app.max_client import MaxClient, OpCode


class CaptureWS:
    def __init__(self):
        self.closed = False
        self.sent = []

    async def send_str(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True


def _client_replying(payload_for_op):
    """A client whose requests are answered immediately with a payload."""
    c = MaxClient(token="t", device_id="d")
    ws = CaptureWS()
    c._ws = ws
    orig = ws.send_str

    async def send_and_reply(raw):
        await orig(raw)
        pkt = json.loads(raw)
        payload = payload_for_op(pkt["opcode"], pkt["payload"])
        asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(
            c._handle({"cmd": 1, "seq": pkt["seq"], "opcode": pkt["opcode"],
                       "payload": payload})))

    ws.send_str = send_and_reply
    return c, ws


class TestMessages:
    async def test_send_message_with_reply_link(self):
        c, ws = _client_replying(lambda op, p: {"message": {"id": 999}})
        resp = await c.send_message(-5, "hi", reply_to=123)
        msg = ws.sent[0]["payload"]["message"]
        assert msg["link"] == {"type": "REPLY", "messageId": "123"}
        assert MaxClient.sent_message_id(resp) == "999"

    async def test_send_message_without_reply_has_no_link(self):
        c, ws = _client_replying(lambda op, p: {})
        await c.send_message(-5, "hi")
        assert "link" not in ws.sent[0]["payload"]["message"]

    def test_sent_message_id_edge_cases(self):
        assert MaxClient.sent_message_id(None) is None
        assert MaxClient.sent_message_id({}) is None
        assert MaxClient.sent_message_id({"_max_error": {}}) is None

    async def test_edit_message_payload(self):
        c, ws = _client_replying(lambda op, p: {"ok": True})
        await c.edit_message(-5, 77, "new", elements=[{"type": "STRONG"}])
        pkt = ws.sent[0]
        assert pkt["opcode"] == OpCode.EDIT_MESSAGE == 67
        assert pkt["payload"] == {"chatId": -5, "messageId": "77", "text": "new",
                                  "elements": [{"type": "STRONG"}], "attachments": []}

    async def test_delete_messages_payload(self):
        c, ws = _client_replying(lambda op, p: {"ok": True})
        await c.delete_messages(-5, [77])
        pkt = ws.sent[0]
        assert pkt["opcode"] == OpCode.DELETE_MESSAGE == 66
        assert pkt["payload"] == {"chatId": -5, "messageIds": ["77"], "forMe": False}


class TestDownloadUrls:
    async def test_video_url_picks_highest_mp4(self):
        c, ws = _client_replying(lambda op, p: {
            "MP4_480": "https://v/480", "MP4_1080": "https://v/1080",
            "MP4_720": "https://v/720", "cache": True, "EXTERNAL": "https://ext",
        })
        url = await c.video_download_url(11, -5, "m1")
        assert url == "https://v/1080"
        assert ws.sent[0]["payload"] == {"videoId": 11, "chatId": -5, "messageId": "m1"}

    async def test_video_url_error(self):
        c, _ = _client_replying(lambda op, p: {"error": "x"})
        c2, _ = _client_replying(lambda op, p: {})
        assert await c.video_download_url(1, 1, "m") is None
        assert await c2.video_download_url(1, 1, "m") is None

    async def test_file_download_url(self):
        c, ws = _client_replying(lambda op, p: {"url": "https://f/1"})
        assert await c.file_download_url(5, -5, "m") == "https://f/1"
        assert ws.sent[0]["opcode"] == OpCode.FILE_DOWNLOAD_URL == 88


class TestUploadReady:
    async def test_video_ready_resolves_video_future(self):
        c = MaxClient(token="t", device_id="d")
        fut = asyncio.get_running_loop().create_future()
        c._video_pending[42] = fut
        await c._handle({"cmd": 0, "seq": 1, "opcode": OpCode.UPLOAD_READY,
                         "payload": {"videoId": 42}})
        assert fut.done()

    async def test_file_ready_resolves_file_future(self):
        c = MaxClient(token="t", device_id="d")
        fut = asyncio.get_running_loop().create_future()
        c._file_pending[43] = fut
        await c._handle({"cmd": 0, "seq": 1, "opcode": OpCode.UPLOAD_READY,
                         "payload": {"fileId": 43}})
        assert fut.done()

    async def test_bad_json_packet_does_not_raise(self):
        # run() skips undecodable packets; _handle itself gets dicts only.
        c = MaxClient(token="t", device_id="d")
        await c._handle({"cmd": 0, "opcode": 999, "payload": {}})


class TestSizeCappedDownload:
    async def _server(self, body: bytes, chunked: bool):
        async def handler(request):
            if chunked:
                resp = web.StreamResponse()
                resp.enable_chunked_encoding()
                await resp.prepare(request)
                for i in range(0, len(body), 1000):
                    await resp.write(body[i:i + 1000])
                await resp.write_eof()
                return resp
            return web.Response(body=body)
        app = web.Application()
        app.router.add_get("/f", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}/f"

    async def test_under_limit_downloads(self, monkeypatch):
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        runner, url = await self._server(b"x" * 5000, chunked=False)
        try:
            c = MaxClient(token="t", device_id="d")
            assert await c.download_file(url, max_bytes=10_000) == b"x" * 5000
        finally:
            await runner.cleanup()

    async def test_declared_size_over_limit_skipped(self):
        runner, url = await self._server(b"x" * 5000, chunked=False)
        try:
            c = MaxClient(token="t", device_id="d")
            assert await c.download_file(url, max_bytes=1000) is None
        finally:
            await runner.cleanup()

    async def test_streamed_size_over_limit_aborted(self):
        runner, url = await self._server(b"x" * 5000, chunked=True)
        try:
            c = MaxClient(token="t", device_id="d")
            assert await c.download_file(url, max_bytes=1000) is None
        finally:
            await runner.cleanup()


def test_proxy_passed_to_client():
    assert MaxClient(token="t", device_id="d").proxy is None
    assert MaxClient(token="t", device_id="d", proxy="http://p:1").proxy == "http://p:1"
