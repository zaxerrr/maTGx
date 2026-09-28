"""Tests for app/tg_sender.py — HTML-safe splitting and send behaviour."""

import re
from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from app.tg_sender import (
    TG_CAPTION_MAX,
    TG_MAX_LENGTH,
    TelegramSender,
    _tg_len,
    split_html,
)


def _balanced(chunk: str) -> bool:
    """Every opened tag is closed in order and no entity/tag is cut."""
    stack = []
    for tag in re.findall(r"<[^>]*>", chunk):
        name = re.match(r"</?\s*(\w+)", tag).group(1)
        if tag.startswith("</"):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    if stack:
        return False
    # No dangling '&' (cut entity) and no dangling '<' (cut tag).
    no_entities = re.sub(r"&#?\w+;", "", chunk)
    no_tags = re.sub(r"<[^>]*>", "", no_entities)
    return "&" not in no_tags and "<" not in no_tags and ">" not in no_tags


class TestSplitHtml:
    def test_short_text_is_unchanged(self):
        assert split_html("<b>hi</b> there", 100) == ["<b>hi</b> there"]

    def test_all_chunks_within_limit_and_balanced(self):
        text = "✉ <b>Иван</b>\n" + "a &amp; b &lt;c&gt; " * 800
        chunks = split_html(text, TG_MAX_LENGTH)
        assert len(chunks) > 1
        for c in chunks:
            assert _tg_len(c) <= TG_MAX_LENGTH
            assert _balanced(c), c[-50:]

    def test_no_content_is_lost(self):
        text = "<b>head</b>\n" + " ".join(f"w{i}" for i in range(3000))
        chunks = split_html(text, 500)
        joined = re.sub(r"<[^>]*>", "", " ".join(chunks))
        for i in (0, 1500, 2999):
            assert f"w{i}" in joined

    def test_open_tag_is_closed_and_reopened(self):
        text = "<i>" + "x " * 100 + "</i>"
        chunks = split_html(text, 60)
        assert len(chunks) > 1
        for c in chunks:
            assert c.startswith("<i>") and c.endswith("</i>")
            assert _tg_len(c) <= 60

    def test_prefers_newline_boundary(self):
        text = "a" * 10 + " " + "b" * 4 + "\n" + "y" * 30
        assert split_html(text, 25)[0] == "a" * 10 + " " + "b" * 4

    def test_entity_never_split(self):
        text = "&amp;" * 50
        for c in split_html(text, 12):
            assert _balanced(c)

    def test_counts_utf16_units(self):
        text = "😀" * 3000  # 6000 UTF-16 units
        chunks = split_html(text, TG_MAX_LENGTH)
        assert len(chunks) == 2
        assert all(_tg_len(c) <= TG_MAX_LENGTH for c in chunks)


def _sender():
    s = TelegramSender.__new__(TelegramSender)
    s._bot = MagicMock()
    s._bot.send_message = AsyncMock(return_value=MagicMock())
    s._bot.send_photo = AsyncMock(return_value=MagicMock())
    s._chat_id = "-100"
    return s


class TestSend:
    async def test_long_text_sent_in_several_messages(self):
        s = _sender()
        await s.send("<b>h</b>\n" + "word " * 2000, message_thread_id=5)
        assert s._bot.send_message.await_count >= 3
        for call in s._bot.send_message.await_args_list:
            assert _tg_len(call.kwargs["text"]) <= TG_MAX_LENGTH
            assert call.kwargs["message_thread_id"] == 5

    async def test_long_caption_overflow_goes_to_text(self):
        s = _sender()
        await s.send_photo(b"img", caption="<b>h</b>\n" + "word " * 400,
                           message_thread_id=3)
        cap = s._bot.send_photo.await_args.kwargs["caption"]
        assert _tg_len(cap) <= TG_CAPTION_MAX
        assert s._bot.send_message.await_count == 1

    async def test_short_caption_single_call(self):
        s = _sender()
        await s.send_photo(b"img", caption="hello")
        assert s._bot.send_photo.await_args.kwargs["caption"] == "hello"
        s._bot.send_message.assert_not_awaited()

    async def test_bad_request_is_not_retried(self):
        s = _sender()
        s._bot.send_message = AsyncMock(side_effect=BadRequest("Can't parse entities"))
        await s.send("x")
        assert s._bot.send_message.await_count == 1
