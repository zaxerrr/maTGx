import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

DEBUG_DIR = "debug"


def _log_task_exception(task: "asyncio.Task") -> None:
    """Done-callback that logs any exception raised by a fire-and-forget task."""
    if not task.cancelled() and task.exception() is not None:
        log.exception("Unhandled exception in background task", exc_info=task.exception())

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}

_WS_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

_HTTP_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Referer": "https://web.max.ru/",
    "Accept": "*/*",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    # Override the browser default to avoid brotli/zstd: aiohttp ships without
    # these codecs by default, and the MAX upload server returns its JSON body
    # brotli-encoded when "br" is advertised.
    "Accept-Encoding": "gzip, deflate",
}


class OpCode(IntEnum):
    HEARTBEAT_PING = 1
    HANDSHAKE = 6
    AUTH_SNAPSHOT = 19
    LOGOUT = 20
    STICKER_STORE = 27
    ASSET_GET = 28
    FAVORITE_STICKER = 29
    CONTACT_GET = 32
    CONTACT_PRESENCE = 35
    CHAT_GET = 48
    SEND_MESSAGE = 64
    ATTACH_TYPING = 65        # "I'm uploading <type> in this chat"
    EDIT_MESSAGE = 67
    DELETE_MESSAGE = 66
    PHOTO_UPLOAD_URL = 80     # get URL for photo upload
    VIDEO_UPLOAD_URL = 82     # get URL for video upload
    VIDEO_DOWNLOAD_URL = 83   # resolve playable URLs of an incoming video
    AUDIO_UPLOAD_URL = 86     # get URL for voice/audio upload (experimental)
    FILE_UPLOAD_URL = 87      # get URL for file upload
    FILE_DOWNLOAD_URL = 88    # resolve download URL of an incoming file
    DISPATCH = 128
    UPLOAD_READY = 136        # server says an uploaded file/video is processed


@dataclass
class MaxMessage:
    chat_id: Any = None
    sender_id: Any = None
    text: str = ""
    timestamp: Any = None
    message_id: str = ""
    is_self: bool = False
    attaches: list = field(default_factory=list)
    link: dict = field(default_factory=dict)
    elements: list = field(default_factory=list)   # text formatting ranges
    raw: dict = field(default_factory=dict)


def _mask_token(obj):
    """Return a copy of a packet/payload with any ``token`` values masked,
    so auth credentials never end up in logs or debug dumps."""
    if isinstance(obj, dict):
        return {k: ("***" if k == "token" and isinstance(v, str) else _mask_token(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_token(v) for v in obj]
    return obj


class MaxClient:
    WS_URL = "wss://ws-api.oneme.ru/websocket"
    HEARTBEAT_SEC = 30
    PING_TIMEOUT_SEC = 15      # no reply to a heartbeat ping → connection is dead
    AUTH_TIMEOUT_SEC = 30      # no AUTH_SNAPSHOT after connect → token is likely stale
    RECONNECT_SEC = 5
    MAX_RECONNECT_SEC = 300    # backoff cap while the connection keeps failing

    def __init__(self, token: str, device_id: str, chat_ids: str | None = None, debug: bool = False):
        self.token = token
        self.device_id = device_id
        self.debug = debug
        self.chat_ids: list[int] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._seq = 0
        self._my_id = None
        self._on_ready_cb = None
        self._on_message_cb = None
        self._heartbeat_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._dispatch_counter = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._file_pending: dict[int, asyncio.Future] = {}
        self._video_pending: dict[int, asyncio.Future] = {}
        self._on_disconnect_cb = None
        self._on_auth_failed_cb = None
        self._authorized = False
        self._reconnect_delay = self.RECONNECT_SEC
        if chat_ids:
            self.chat_ids += map(int, map(str.strip, chat_ids.split(',')))

    # ── decorator API ──────────────────────────────────────────────

    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
        return func

    def on_auth_failed(self, func):
        """Called with a human-readable reason when MAX rejects the token or
        never completes authorization."""
        self._on_auth_failed_cb = func
        return func

    # ── transport ──────────────────────────────────────────────────

    async def _send(self, opcode: int, payload: dict) -> int:
        if not self._ws or self._ws.closed:
            return -1
        seq = self._seq
        pkt = {
            "ver": 11,
            "cmd": 0,
            "seq": seq,
            "opcode": opcode,
            "payload": payload,
        }
        self._seq += 1
        raw = json.dumps(pkt, ensure_ascii=False)
        if log.isEnabledFor(logging.DEBUG):
            masked = json.dumps(_mask_token(pkt), ensure_ascii=False)
            log.debug(">>> SEND op=%d seq=%d | %s", opcode, seq, masked[:800])
        await self._ws.send_str(raw)
        return seq

    async def _request(self, opcode: int, payload: dict, timeout: float) -> dict:
        """Send a request and wait for its response (same seq).

        Raises ``asyncio.TimeoutError`` if no response arrives in time and
        ``ConnectionError`` if the socket is not open.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        seq = await self._send(opcode, payload)
        if seq < 0:
            raise ConnectionError("MAX WebSocket is not connected")
        self._pending[seq] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(seq, None)

    async def cmd(self, opcode: int, payload: dict, timeout: float = 10) -> dict:
        """Send a request and wait for the response (cmd=1 with same seq).

        Returns ``{}`` on timeout or when not connected.
        """
        try:
            return await self._request(opcode, payload, timeout)
        except asyncio.TimeoutError:
            log.warning("cmd timeout: op=%d", opcode)
            return {}
        except ConnectionError:
            log.warning("cmd op=%d skipped: MAX is not connected", opcode)
            return {}

    async def _heartbeat_loop(self, ws, connected_at: float):
        """Keep the connection alive and tear it down when it is stuck.

        - AUTH_SNAPSHOT never arrived → the token was most likely rotated
          (the server accepts the handshake but stays silent).
        - A heartbeat ping got no reply → the TCP connection is half-open.

        Closing ``ws`` ends the read loop in ``run()``, which reconnects.
        """
        loop = asyncio.get_running_loop()
        while not ws.closed:
            if not self._authorized:
                remaining = connected_at + self.AUTH_TIMEOUT_SEC - loop.time()
                if remaining <= 0:
                    await self._auth_failed(
                        f"сервер не прислал AUTH_SNAPSHOT за {self.AUTH_TIMEOUT_SEC} с"
                    )
                    break
                await asyncio.sleep(min(remaining, self.HEARTBEAT_SEC))
                continue
            await asyncio.sleep(self.HEARTBEAT_SEC)
            if ws.closed:
                break
            try:
                await self._request(OpCode.HEARTBEAT_PING, {"interactive": False},
                                    timeout=self.PING_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                log.warning("Heartbeat ping got no reply in %ds — reconnecting",
                            self.PING_TIMEOUT_SEC)
                await ws.close()
                break
            except Exception:
                log.exception("Heartbeat error — reconnecting")
                await ws.close()
                break

    async def _auth_failed(self, reason: str) -> None:
        log.error("MAX authorization failed: %s. Refresh MAX_TOKEN in .env "
                  "(web.max.ru → Local Storage → __oneme_auth).", reason)
        if self._on_auth_failed_cb:
            task = asyncio.create_task(self._on_auth_failed_cb(reason))
            task.add_done_callback(_log_task_exception)
        if self._ws and not self._ws.closed:
            await self._ws.close()

    # ── main loop ──────────────────────────────────────────────────

    async def run(self):
        if self.debug:
            os.makedirs(DEBUG_DIR, exist_ok=True)

        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            self._session = session
            while True:
                try:
                    log.info("Connecting to %s ...", self.WS_URL)
                    async with session.ws_connect(
                        self.WS_URL, headers=_WS_HEADERS
                    ) as ws:
                        self._ws = ws
                        self._seq = 0
                        self._pending.clear()
                        self._authorized = False

                        log.info("Connected. Sending handshake...")
                        await self._send(
                            OpCode.HANDSHAKE,
                            {
                                "deviceId": self.device_id,
                                "userAgent": {
                                    "deviceType": "WEB",
                                    "deviceName": "Chrome 131.0.0.0",
                                },
                                "appVersion": "26.2.2",
                            },
                        )

                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop(ws, asyncio.get_running_loop().time())
                        )

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    data = json.loads(msg.data)
                                except ValueError:
                                    log.warning("Undecodable packet from MAX: %.200s", msg.data)
                                    continue
                                await self._handle(data)
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                log.warning("WebSocket closed/error: %s", msg.type)
                                break

                except Exception:
                    log.exception("Connection error")

                finally:
                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.cancel()
                    self._pending.clear()

                if self._on_disconnect_cb:
                    try:
                        await self._on_disconnect_cb()
                    except Exception:
                        log.exception("on_disconnect callback error")

                # Successful auth resets the delay; repeated failures (bad
                # token, network down) back off so we don't hammer MAX.
                delay = self._next_reconnect_delay()
                log.info("Reconnecting in %ds...", delay)
                await asyncio.sleep(delay)

    def _next_reconnect_delay(self) -> int:
        if self._authorized:
            self._reconnect_delay = self.RECONNECT_SEC
        # Consumed: failed connects after this (e.g. network down) must keep
        # backing off instead of resetting on a stale flag.
        self._authorized = False
        delay = self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, self.MAX_RECONNECT_SEC)
        return delay

    # ── event dispatcher ───────────────────────────────────────────

    async def _handle(self, data: dict):
        op = data.get("opcode")
        cmd = data.get("cmd")
        seq = data.get("seq")
        payload = data.get("payload", {})

        # cmd=1 is a response to our request — resolve the pending future
        if cmd == 1 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result(payload)
            if op not in (OpCode.HANDSHAKE, OpCode.AUTH_SNAPSHOT):
                log.debug("<<< RESP  op=%-4s seq=%s", op, seq)

        # cmd=3 is an error response
        elif cmd == 3 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result({"_max_error": payload})
            log.warning("<<< ERROR op=%-4s seq=%s | %s", op, seq, payload)

        # server-initiated events — not a reply to one of our requests
        else:
            payload_preview = json.dumps(payload, ensure_ascii=False)
            if len(payload_preview) > 3000:
                payload_preview = payload_preview[:3000] + "…"

            if op == OpCode.HANDSHAKE and cmd == 1:
                log.info("Handshake OK → sending auth token...")
                await self._send(
                    OpCode.AUTH_SNAPSHOT,
                    {
                        "chatsCount": 10,
                        "interactive": True,
                        "token": self.token,
                    },
                )

            elif op in (OpCode.HANDSHAKE, OpCode.AUTH_SNAPSHOT) and cmd == 3:
                await self._auth_failed(
                    f"сервер отклонил {'handshake' if op == OpCode.HANDSHAKE else 'токен'}: "
                    f"{payload_preview[:300]}"
                )

            elif op == OpCode.AUTH_SNAPSHOT and cmd == 1:
                self._authorized = True
                self._my_id = payload.get("profile", {}).get("id")
                log.info("Authorized! my_id=%s", self._my_id)
                if self.debug:
                    self._dump_json("snapshot.json", payload)

                if self._on_ready_cb:
                    # Run as a task: the callback issues RPCs (contact lookup)
                    # whose replies are read by this very loop — awaiting it
                    # here would block until every such request timed out.
                    task = asyncio.create_task(self._on_ready_cb(payload))
                    task.add_done_callback(_log_task_exception)

            elif op == OpCode.DISPATCH:
                self._dispatch_counter += 1
                if self.debug and self._dispatch_counter <= 20:
                    self._dump_json(
                        f"dispatch_{self._dispatch_counter:04d}.json", payload
                    )

                if self._on_message_cb:
                    msg = self._parse_message(payload)
                    if msg is not None and ((not self.chat_ids) or (msg.chat_id in self.chat_ids)):
                        task = asyncio.create_task(self._on_message_cb(msg))
                        task.add_done_callback(_log_task_exception)

            elif op == OpCode.UPLOAD_READY:
                # Server confirms an uploaded file/video finished server-side processing.
                fut = None
                if payload.get("videoId") is not None:
                    fut = self._video_pending.pop(int(payload["videoId"]), None)
                elif payload.get("fileId") is not None:
                    fut = self._file_pending.pop(int(payload["fileId"]), None)
                if fut and not fut.done():
                    fut.set_result(payload)
                log.debug("UPLOAD_READY op=136: %s", payload)

            elif op in (OpCode.HEARTBEAT_PING,):
                log.debug("Heartbeat op=%s", op)

            elif cmd not in (1, 3):
                log.info("<<< EVENT op=%-4s cmd=%-3s | %s", op, cmd, payload_preview[:500])

    # ── WebSocket RPC: fetch contacts ──────────────────────────────

    async def fetch_contacts(self, contact_ids: list[int]) -> dict:
        """Fetch contact info via WS opcode 32. Returns raw response payload."""
        if not contact_ids:
            return {}
        resp = await self.cmd(OpCode.CONTACT_GET, {"contactIds": contact_ids})
        if self.debug:
            self._dump_json("contacts_response.json", resp)
        log.info("fetch_contacts(%s) → keys: %s", contact_ids, list(resp.keys()))
        return resp

    async def send_message(self, chat_id, text: str = "", elements=None,
                            attaches=None, reply_to=None) -> dict:
        """Send a message to a Max chat. Returns the server response.

        Both ``elements`` (text formatting) and ``attaches`` (photos, files,
        voice, ...) are optional. Pass an empty ``text`` together with an
        attach to send a media-only message. ``reply_to`` is a Max message
        id to reply to.
        """
        if elements is None:
            elements = []
        if attaches is None:
            attaches = []
        cid = int(time.time() * 1000) * 1000 + random.randint(0, 999)
        message = {"text": text, "cid": cid, "elements": elements}
        if attaches:
            message["attaches"] = attaches
        if reply_to is not None:
            message["link"] = {"type": "REPLY", "messageId": str(reply_to)}
        resp = await self.cmd(
            OpCode.SEND_MESSAGE,
            {
                "chatId": chat_id,
                "message": message,
                "notify": True,
            },
        )
        ok = bool(resp) and "_max_error" not in resp
        log.info("send_message(chat=%s, attaches=%d) → %s",
                 chat_id, len(attaches), "OK" if ok else "FAIL")
        return resp

    @staticmethod
    def sent_message_id(resp: dict | None) -> str | None:
        """Max message id from a SEND_MESSAGE response, if present."""
        if not isinstance(resp, dict) or "_max_error" in resp:
            return None
        msg = resp.get("message")
        if isinstance(msg, dict) and msg.get("id") is not None:
            return str(msg["id"])
        return None

    async def edit_message(self, chat_id, message_id, text: str,
                           elements=None) -> dict:
        """Edit the text of a message we sent (opcode 67)."""
        resp = await self.cmd(OpCode.EDIT_MESSAGE, {
            "chatId": chat_id,
            "messageId": str(message_id),
            "text": text,
            "elements": elements or [],
            "attachments": [],
        })
        log.info("edit_message(chat=%s, msg=%s) → %s", chat_id, message_id,
                 "OK" if resp and "_max_error" not in resp else "FAIL")
        return resp

    async def delete_messages(self, chat_id, message_ids: list,
                              for_me: bool = False) -> dict:
        """Delete messages (opcode 66). ``for_me=False`` deletes for everyone."""
        resp = await self.cmd(OpCode.DELETE_MESSAGE, {
            "chatId": chat_id,
            "messageIds": [str(m) for m in message_ids],
            "forMe": for_me,
        })
        log.info("delete_messages(chat=%s, %s) → %s", chat_id, message_ids,
                 "OK" if resp and "_max_error" not in resp else "FAIL")
        return resp

    # ── media download (incoming) ──────────────────────────────────

    async def video_download_url(self, video_id, chat_id, message_id) -> str | None:
        """Resolve a playable MP4 URL for an incoming VIDEO attach (op 83).

        The response maps quality keys (``MP4_720``, ``MP4_480``, ...) to
        URLs, plus service keys like ``cache`` / ``EXTERNAL``.
        """
        resp = await self.cmd(OpCode.VIDEO_DOWNLOAD_URL, {
            "videoId": video_id,
            "chatId": chat_id,
            "messageId": str(message_id),
        })
        if not resp or "_max_error" in resp:
            log.warning("video_download_url failed: %s", str(resp)[:300])
            return None
        mp4 = {k: v for k, v in resp.items()
               if isinstance(v, str) and v.startswith("http") and k.upper().startswith("MP4")}

        def quality(key: str) -> int:
            digits = "".join(ch for ch in key if ch.isdigit())
            return int(digits) if digits else 0

        # Highest quality first; the size limit is enforced on download.
        for key in sorted(mp4, key=quality, reverse=True):
            return mp4[key]
        for v in resp.values():
            if isinstance(v, str) and v.startswith("http"):
                return v
        return None

    async def file_download_url(self, file_id, chat_id, message_id) -> str | None:
        """Resolve a download URL for an incoming FILE attach (op 88)."""
        resp = await self.cmd(OpCode.FILE_DOWNLOAD_URL, {
            "fileId": file_id,
            "chatId": chat_id,
            "messageId": str(message_id),
        })
        url = resp.get("url") if resp and "_max_error" not in resp else None
        if not (isinstance(url, str) and url.startswith("http")):
            log.warning("file_download_url failed: %s", str(resp)[:300])
            return None
        return url

    # ── media upload ───────────────────────────────────────────────

    async def upload_photo(self, data: bytes, chat_id=None,
                            filename: str = "image.jpg",
                            mimetype: str = "image/jpeg") -> dict | None:
        """Upload a photo and return an attach dict for send_message.

        Protocol (reverse-engineered, see nsdkinx/vkmax):
          1. opcode 80 → response.payload.url
          2. (optional) opcode 65 → 'I'm uploading PHOTO' indicator
          3. multipart POST file to URL → JSON {'photos': {key: {'token': ...}}}
          4. token → {"_type": "PHOTO", "photoToken": token}
        """
        resp = await self.cmd(OpCode.PHOTO_UPLOAD_URL, {"count": 1})
        if not resp or "_max_error" in resp:
            log.error("Photo upload URL request failed: %s", resp)
            return None
        url = resp.get("url")
        if not url:
            log.error("Photo upload response missing 'url': %s", resp)
            return None

        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING,
                           {"chatId": chat_id, "type": "PHOTO"})

        body = await self._http_upload(url, data, filename, mimetype,
                                        expect_json=True)
        if not isinstance(body, dict):
            return None

        photos = body.get("photos") or {}
        if not photos:
            log.error("Photo upload OK but no 'photos' in response: %s", body)
            return None
        first = next(iter(photos.values()))
        token = first.get("token") if isinstance(first, dict) else None
        if not token:
            log.error("Photo upload response missing token: %s", body)
            return None
        return {"_type": "PHOTO", "photoToken": token}

    async def open_by_link(self, link: str) -> dict:
        """Resolve a max.ru invite link via opcode 57.

        Works for ``/join/<token>`` (group / channel invite — chat namespace).
        ``/u/<token>`` (user share link) needs a different, undocumented
        opcode that we haven't reverse-engineered yet — server returns
        ``not.found`` for chat-namespace lookup.
        """
        resp = await self.cmd(57, {"link": link})
        log.info("open_by_link(%s) → %s",
                 link[:60], str(resp)[:300] if resp else resp)
        return resp

    async def upload_audio(self, data: bytes, chat_id=None,
                            filename: str = "voice.ogg",
                            mimetype: str = "audio/ogg",
                            timeout: float = 60.0) -> dict | None:
        """Upload a voice / audio message.

        Known limitation: MAX stores audio in a different namespace from
        regular files; ``fileId`` from the file-upload path is rejected by
        the audio service ("EJB service unavailable"). Until we figure out
        the dedicated audio-upload endpoint, voice arrives in MAX as an
        ``.ogg`` file rather than a voice bubble.
        """
        return await self.upload_file(
            data, chat_id=chat_id, filename=filename, mimetype=mimetype,
            attach_type="FILE", timeout=timeout,
        )

    async def upload_file(self, data: bytes, chat_id=None,
                           filename: str = "file.bin",
                           mimetype: str = "application/octet-stream",
                           attach_type: str = "FILE",
                           timeout: float = 60.0) -> dict | None:
        """Upload a generic file (used for voice, audio, documents, video).

        Unlike photos, the server processes the file asynchronously and only
        then sends an opcode-136 UPLOAD_READY event. We wait for that event
        before returning the attach dict — otherwise the SEND_MESSAGE that
        follows would reference a file the server hasn't finished ingesting.
        """
        resp = await self.cmd(OpCode.FILE_UPLOAD_URL, {"count": 1})
        if not resp or "_max_error" in resp:
            log.error("File upload URL request failed: %s", resp)
            return None
        info_list = resp.get("info") or []
        if not info_list:
            log.error("File upload response missing 'info': %s", resp)
            return None
        info = info_list[0]
        url = info.get("url")
        file_id = info.get("fileId")
        if not url or file_id is None:
            log.error("File upload info missing url/fileId: %s", info)
            return None

        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING,
                           {"chatId": chat_id, "type": attach_type})

        # Register the pending future BEFORE the POST so the server's reply
        # (which may arrive before our POST coroutine resumes) is not lost.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._file_pending[int(file_id)] = fut

        ok = await self._http_upload(url, data, filename, mimetype,
                                      expect_json=False)
        if not ok:
            self._file_pending.pop(int(file_id), None)
            return None

        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._file_pending.pop(int(file_id), None)
            log.warning("File upload server processing timed out (fileId=%s)",
                        file_id)
            return None

        return {"_type": attach_type, "fileId": file_id}

    async def upload_video(self, data: bytes, chat_id=None,
                           filename: str = "video.mp4",
                           mimetype: str = "video/mp4",
                           timeout: float = 120.0) -> dict | None:
        """Upload a video as a native MAX video (opcode 82).

        Like files, the server confirms processing with an UPLOAD_READY
        (op 136) event carrying the ``videoId``.
        """
        resp = await self.cmd(OpCode.VIDEO_UPLOAD_URL, {"count": 1})
        info_list = (resp or {}).get("info") or []
        if not info_list or "_max_error" in resp:
            log.error("Video upload URL request failed: %s", resp)
            return None
        info = info_list[0]
        url, video_id, token = info.get("url"), info.get("videoId"), info.get("token")
        if not url or video_id is None:
            log.error("Video upload info missing url/videoId: %s", info)
            return None

        if chat_id is not None:
            await self.cmd(OpCode.ATTACH_TYPING, {"chatId": chat_id, "type": "VIDEO"})

        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._video_pending[int(video_id)] = fut
        ok = await self._http_upload(url, data, filename, mimetype, expect_json=False)
        if not ok:
            self._video_pending.pop(int(video_id), None)
            return None
        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._video_pending.pop(int(video_id), None)
            log.warning("Video processing timed out (videoId=%s)", video_id)
            return None

        attach = {"_type": "VIDEO", "videoId": video_id}
        if token:
            attach["token"] = token
        return attach

    async def _http_upload(self, url: str, data: bytes, filename: str,
                            mimetype: str, expect_json: bool):
        """POST multipart bytes to an upload URL. Returns parsed JSON if
        ``expect_json`` is True, otherwise True on HTTP 200, or None on error.
        """
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            form = aiohttp.FormData()
            form.add_field("file", data, filename=filename, content_type=mimetype)
            async with session.post(
                url, headers=_HTTP_HEADERS, data=form,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:500]
                    log.error("Upload POST failed: HTTP %d — %s",
                              resp.status, body)
                    return None
                if expect_json:
                    try:
                        return await resp.json()
                    except Exception:
                        log.exception("Upload response is not JSON")
                        return None
                # consume body to release connection
                await resp.read()
                return True
        except Exception:
            log.exception("Upload POST error: %s", url[:120])
            return None
        finally:
            if close_after:
                await session.close()

    async def download_file(self, url: str, max_bytes: int | None = None) -> bytes | None:
        """Download a file by URL, returning raw bytes or None on failure.

        With ``max_bytes``, bail out (return None) as soon as the declared or
        actual size exceeds it, instead of buffering a huge file in memory.
        """
        session = getattr(self, "_session", None)
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True
        try:
            async with session.get(
                url, headers=_HTTP_HEADERS,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status == 200:
                    if max_bytes is not None:
                        declared = resp.content_length
                        if declared is not None and declared > max_bytes:
                            log.info("Skip download %s: %d bytes > limit %d",
                                     url[:120], declared, max_bytes)
                            return None
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            buf += chunk
                            if len(buf) > max_bytes:
                                log.info("Abort download %s: over limit %d",
                                         url[:120], max_bytes)
                                return None
                        data = bytes(buf)
                    else:
                        data = await resp.read()
                    log.info("Downloaded %s (%d bytes)", url[:120], len(data))
                    return data
                log.warning("Download failed %s — HTTP %d", url[:120], resp.status)
        except Exception:
            log.exception("Download error: %s", url[:120])
        finally:
            if close_after:
                await session.close()
        return None

    # ── message parsing ────────────────────────────────────────────

    def _parse_message(self, payload: dict) -> MaxMessage | None:
        msg_body = payload.get("message")
        if not msg_body or not isinstance(msg_body, dict):
            return None

        msg = MaxMessage(
            chat_id=payload.get("chatId"),
            sender_id=msg_body.get("sender"),
            text=msg_body.get("text", ""),
            timestamp=msg_body.get("time"),
            message_id=str(msg_body.get("id", "")),
            attaches=msg_body.get("attaches") or [],
            link=msg_body.get("link") or {},
            elements=msg_body.get("elements") or [],
            raw=payload,
        )

        if self._my_id and msg.sender_id == self._my_id:
            msg.is_self = True

        return msg

    # ── debug helpers ──────────────────────────────────────────────

    @staticmethod
    def _dump_json(filename: str, data: dict) -> None:
        path = os.path.join(DEBUG_DIR, filename)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(_mask_token(data), f, ensure_ascii=False, indent=2)
            log.info("Dumped %s (%d bytes)", path, os.path.getsize(path))
        except Exception:
            log.exception("Failed to dump %s", path)
