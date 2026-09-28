"""End-to-end-ish tests for the MAX → Telegram message flow in
app/max_listener.py: ordering, dedupe/edits, native replies, size limits."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.max_client import MaxMessage
from app.max_listener import create_max_client
from app.msgmap import MessageMap
from app.tg_sender import TG_UPLOAD_LIMIT


class FakeSender:
    """Records what would be sent to Telegram; returns fake messages."""

    def __init__(self):
        self.calls = []
        self._next_id = 100
        self.topic_store = MagicMock()
        self.topic_store.get_topic = MagicMock(return_value=5)
        self.bot = MagicMock()
        self.chat_id = "-100"
        self.on_topic_recreated = None
        self.edit_text = AsyncMock(return_value=True)
        self.ensure_topic = AsyncMock(return_value=5)

    def _msg(self):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    def _rec(kind):
        async def fn(self, *args, delay=0, **kw):
            self.calls.append((kind, args, kw))
            return self._msg()
        return fn

    send = _rec("text")
    send_photo = _rec("photo")
    send_video = _rec("video")
    send_document = _rec("document")
    send_voice = _rec("voice")
    send_sticker = _rec("sticker")


def _setup(tmp_path):
    sender = FakeSender()
    mm = MessageMap(str(tmp_path / "m.db"))
    client = create_max_client("t", "d", sender, msgmap=mm)
    client.resolver.resolve_user = AsyncMock(return_value="Ann")
    return client, sender, mm


def _msg(mid, text="hi", chat=-7, attaches=None, link=None):
    return MaxMessage(chat_id=chat, sender_id=3, text=text, message_id=str(mid),
                      attaches=attaches or [], link=link or {})


class TestOrderingAndDedupe:
    async def test_messages_of_one_chat_keep_arrival_order(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        slow = asyncio.Event()

        async def slow_download(url, max_bytes=None):
            await slow.wait()
            return b"img"
        client.download_file = slow_download

        t1 = asyncio.create_task(client._on_message_cb(
            _msg(1, text="", attaches=[{"_type": "PHOTO", "baseUrl": "http://p"}])))
        t2 = asyncio.create_task(client._on_message_cb(_msg(2, text="after photo")))
        await asyncio.sleep(0.05)
        assert sender.calls == []          # text waits for the photo
        slow.set()
        await asyncio.gather(t1, t2)
        assert [c[0] for c in sender.calls] == ["photo", "text"]

    async def test_intro_posted_once_for_burst_in_new_chat(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        sender.topic_store.get_topic = MagicMock(return_value=None)

        async def ensure(chat, title):
            sender.topic_store.get_topic = MagicMock(return_value=5)
            return 5
        sender.ensure_topic = ensure
        with patch("app.tg_handler.post_topic_intro", new=AsyncMock()) as intro:
            await asyncio.gather(*(client._on_message_cb(_msg(i)) for i in range(3)))
        intro.assert_awaited_once()

    async def test_redelivery_is_skipped(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        await client._on_message_cb(_msg(1, "same"))
        await client._on_message_cb(_msg(1, "same"))
        assert len(sender.calls) == 1

    async def test_edit_in_max_edits_telegram_message(self, tmp_path):
        client, sender, mm = _setup(tmp_path)
        await client._on_message_cb(_msg(1, "old"))
        tg_id = mm.tg_for_max(-7, "1")
        await client._on_message_cb(_msg(1, "new <text>"))
        sender.edit_text.assert_awaited_once()
        mid, html = sender.edit_text.await_args.args
        assert mid == tg_id and "new &lt;text&gt;" in html and "изменено" in html
        assert mm.text_for_max(-7, "1") == "new <text>"

    async def test_edit_falls_back_to_reply_when_edit_fails(self, tmp_path):
        client, sender, mm = _setup(tmp_path)
        await client._on_message_cb(_msg(1, "old"))
        sender.edit_text = AsyncMock(return_value=False)
        await client._on_message_cb(_msg(1, "new"))
        kind, args, kw = sender.calls[-1]
        assert kind == "text" and "Изменено" in args[0]
        assert kw["reply_to"] == mm.tg_for_max(-7, "1")


class TestReplies:
    async def test_reply_to_bridged_message_is_native(self, tmp_path):
        client, sender, mm = _setup(tmp_path)
        await client._on_message_cb(_msg(1, "question"))
        target = mm.tg_for_max(-7, "1")
        await client._on_message_cb(_msg(2, "answer", link={
            "type": "REPLY", "messageId": "1", "message": {"text": "question"}}))
        kind, args, kw = sender.calls[-1]
        assert kind == "text" and kw["reply_to"] == target
        assert "Ответ на" not in args[0]    # no quoted block needed
        assert len(sender.calls) == 2

    async def test_reply_to_unknown_message_is_quoted(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        await client._on_message_cb(_msg(2, "answer", link={
            "type": "REPLY", "messageId": "zzz", "message": {"text": "old q"}}))
        texts = [c[1][0] for c in sender.calls if c[0] == "text"]
        assert any("Ответ" in t and "old q" in t for t in texts)
        assert any("answer" in t for t in texts)


class TestSizeLimits:
    async def test_big_file_not_downloaded(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        client.download_file = AsyncMock()
        await client._on_message_cb(_msg(1, "", attaches=[{
            "_type": "FILE", "name": "big.iso", "size": TG_UPLOAD_LIMIT + 1,
            "url": "http://f"}]))
        client.download_file.assert_not_awaited()
        assert "слишком большой" in sender.calls[0][1][0]

    async def test_file_without_url_uses_op88(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        client.file_download_url = AsyncMock(return_value="http://f")
        client.download_file = AsyncMock(return_value=b"data")
        await client._on_message_cb(_msg(1, "", attaches=[{
            "_type": "FILE", "name": "a.pdf", "size": 10, "fileId": 9}]))
        client.file_download_url.assert_awaited_once_with(9, -7, "1")
        assert client.download_file.await_args.kwargs["max_bytes"] == TG_UPLOAD_LIMIT
        assert sender.calls[0][0] == "document"

    async def test_video_downloaded_via_op83(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        client.video_download_url = AsyncMock(return_value="http://v")
        client.download_file = AsyncMock(return_value=b"mp4")
        await client._on_message_cb(_msg(1, "", attaches=[{
            "_type": "VIDEO", "videoId": 4, "thumbnail": "http://t"}]))
        assert sender.calls[0][0] == "video"

    async def test_video_falls_back_to_thumbnail(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        client.video_download_url = AsyncMock(return_value=None)
        client.download_file = AsyncMock(return_value=b"jpg")
        await client._on_message_cb(_msg(1, "", attaches=[{
            "_type": "VIDEO", "videoId": 4, "thumbnail": "http://t"}]))
        assert sender.calls[0][0] == "photo"
        assert "превью" in sender.calls[0][2]["caption"]


class TestMaxFormatting:
    def test_basic_styles_and_escaping(self):
        from app.max_listener import max_text_to_html
        text = "bold <i> link"
        html = max_text_to_html(text, [
            {"type": "STRONG", "from": 0, "length": 4},
            {"type": "LINK", "from": 9, "length": 4,
             "attributes": {"url": "https://x.ru/?a=1&b=\"2\""}},
        ])
        assert html == ('<b>bold</b> &lt;i&gt; '
                        '<a href="https://x.ru/?a=1&amp;b=&quot;2&quot;">link</a>')

    def test_overlapping_ranges_stay_well_formed(self):
        from app.max_listener import max_text_to_html
        html = max_text_to_html("abcdef", [
            {"type": "STRONG", "from": 0, "length": 4},
            {"type": "EMPHASIZED", "from": 2, "length": 4},
        ])
        assert html == "<b>ab</b><b><i>cd</i></b><i>ef</i>"

    def test_codepoint_offsets_and_unknown_types(self):
        from app.max_listener import max_text_to_html
        html = max_text_to_html("😀 hi", [
            {"type": "MONOSPACED", "from": 2, "length": 2},
            {"type": "HEADING", "from": 0, "length": 1},
            {"type": "STRONG", "from": 10, "length": 3},
        ])
        assert html == "😀 <code>hi</code>"

    async def test_formatting_reaches_telegram(self, tmp_path):
        client, sender, _ = _setup(tmp_path)
        m = _msg(1, "hello world")
        m.elements = [{"type": "STRONG", "from": 0, "length": 5}]
        await client._on_message_cb(m)
        assert "<b>hello</b> world" in sender.calls[0][1][0]
