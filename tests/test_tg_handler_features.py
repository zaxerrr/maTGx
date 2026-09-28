"""Tests for TG → MAX replies, edits, /rm, media kinds and size checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.msgmap import MessageMap
from app.tg_handler import (
    ALLOWED_USER_KEY,
    MAX_CLIENT_KEY,
    TG_BOT_DOWNLOAD_LIMIT,
    TOPIC_STORE_KEY,
    _cmd_rm,
    _on_topic_edit,
    _on_topic_media,
    _on_topic_message,
    _on_topic_unsupported,
    build_tg_app,
)


def _store():
    s = MagicMock()
    s.chat_for_topic = MagicMock(side_effect=lambda t: {10: 42}.get(t))
    return s


def _client(tmp_path):
    c = MagicMock()
    c.msgmap = MessageMap(str(tmp_path / "m.db"))
    c.send_message = AsyncMock(return_value={"message": {"id": 555}})
    c.edit_message = AsyncMock(return_value={"ok": True})
    c.delete_messages = AsyncMock(return_value={"ok": True})
    return c


def _ctx(client):
    ctx = MagicMock()
    ctx.bot_data = {ALLOWED_USER_KEY: None, MAX_CLIENT_KEY: client,
                    TOPIC_STORE_KEY: _store()}
    return ctx


def _message(mid=1, text="hi", reply_to_id=None, **media):
    m = MagicMock()
    m.message_id = mid
    m.text = text
    m.caption = None
    m.caption_entities = None
    m.entities = None
    m.message_thread_id = 10
    m.is_topic_message = True
    m.reply_text = AsyncMock()
    m.set_reaction = AsyncMock()
    m.reply_to_message = SimpleNamespace(message_id=reply_to_id) if reply_to_id else None
    for kind in ("photo", "sticker", "voice", "audio", "animation", "video_note",
                 "video", "document"):
        setattr(m, kind, media.get(kind))
    return m


def _update(message=None, edited=None):
    u = MagicMock()
    u.message = message
    u.edited_message = edited
    u.effective_user = MagicMock(id=1)
    return u


class TestRepliesAndMapping:
    async def test_sent_message_is_remembered(self, tmp_path):
        c = _client(tmp_path)
        await _on_topic_message(_update(_message(mid=7)), _ctx(c))
        assert c.msgmap.max_for_tg(7) == (42, "555")

    async def test_reply_to_bridged_message_sets_max_reply(self, tmp_path):
        c = _client(tmp_path)
        c.msgmap.add(42, "900", 3, "orig")
        await _on_topic_message(_update(_message(mid=8, reply_to_id=3)), _ctx(c))
        assert c.send_message.await_args.kwargs["reply_to"] == "900"

    async def test_reply_to_topic_root_is_plain_message(self, tmp_path):
        c = _client(tmp_path)
        await _on_topic_message(_update(_message(mid=8, reply_to_id=10)), _ctx(c))
        assert "reply_to" not in c.send_message.await_args.kwargs

    async def test_reply_into_other_chat_mapping_ignored(self, tmp_path):
        c = _client(tmp_path)
        c.msgmap.add(99, "900", 3, "orig")        # belongs to another MAX chat
        await _on_topic_message(_update(_message(mid=8, reply_to_id=3)), _ctx(c))
        assert "reply_to" not in c.send_message.await_args.kwargs


class TestEdits:
    async def test_edit_is_mirrored(self, tmp_path):
        c = _client(tmp_path)
        c.msgmap.add(42, "900", 7, "old")
        await _on_topic_edit(_update(edited=_message(mid=7, text="new")), _ctx(c))
        c.edit_message.assert_awaited_once()
        assert c.edit_message.await_args.args[:3] == (42, "900", "new")
        assert c.msgmap.text_for_max(42, "900") == "new"

    async def test_edit_of_unknown_message_ignored(self, tmp_path):
        c = _client(tmp_path)
        await _on_topic_edit(_update(edited=_message(mid=7, text="new")), _ctx(c))
        c.edit_message.assert_not_awaited()

    async def test_rejected_edit_is_reported(self, tmp_path):
        c = _client(tmp_path)
        c.msgmap.add(42, "900", 7, "old")
        c.edit_message = AsyncMock(return_value={"_max_error": {"message": "too old"}})
        m = _message(mid=7, text="new")
        await _on_topic_edit(_update(edited=m), _ctx(c))
        assert "too old" in m.reply_text.await_args.args[0]


class TestRm:
    async def test_rm_deletes_replied_message(self, tmp_path):
        c = _client(tmp_path)
        c.msgmap.add(42, "900", 3, "x")
        m = _message(mid=9, text="/rm", reply_to_id=3)
        await _cmd_rm(_update(m), _ctx(c))
        c.delete_messages.assert_awaited_once_with(42, ["900"], for_me=False)
        assert "Удалено" in m.reply_text.await_args.args[0]

    async def test_rm_without_reply_explains(self, tmp_path):
        c = _client(tmp_path)
        m = _message(mid=9, text="/rm")
        await _cmd_rm(_update(m), _ctx(c))
        c.delete_messages.assert_not_awaited()
        m.reply_text.assert_awaited_once()


class TestMedia:
    async def test_too_big_file_is_refused_before_download(self, tmp_path):
        c = _client(tmp_path)
        doc = MagicMock(file_size=TG_BOT_DOWNLOAD_LIMIT + 1)
        doc.get_file = AsyncMock()
        m = _message(text=None, document=doc)
        await _on_topic_media(_update(m), _ctx(c))
        doc.get_file.assert_not_awaited()
        assert "20 МБ" in m.reply_text.await_args.args[0]

    def _file(self, data=b"bytes"):
        f = MagicMock(file_size=10, file_name="v.mp4", mime_type="video/mp4")
        tg_file = MagicMock()
        tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(data))
        f.get_file = AsyncMock(return_value=tg_file)
        return f

    async def test_video_uses_native_upload(self, tmp_path):
        c = _client(tmp_path)
        c.upload_video = AsyncMock(return_value={"_type": "VIDEO", "videoId": 1})
        c.upload_file = AsyncMock()
        await _on_topic_media(_update(_message(text=None, video=self._file())), _ctx(c))
        c.upload_file.assert_not_awaited()
        assert c.send_message.await_args.kwargs["attaches"] == [{"_type": "VIDEO", "videoId": 1}]

    async def test_video_falls_back_to_file(self, tmp_path):
        c = _client(tmp_path)
        c.upload_video = AsyncMock(return_value=None)
        c.upload_file = AsyncMock(return_value={"_type": "FILE", "fileId": 2})
        await _on_topic_media(_update(_message(text=None, video_note=self._file())), _ctx(c))
        c.upload_file.assert_awaited_once()

    async def test_animation_checked_before_document(self, tmp_path):
        c = _client(tmp_path)
        c.upload_video = AsyncMock(return_value={"_type": "VIDEO", "videoId": 1})
        c.upload_file = AsyncMock()
        f = self._file()
        await _on_topic_media(_update(_message(text=None, animation=f, document=f)), _ctx(c))
        c.upload_video.assert_awaited_once()

    async def test_static_sticker_sent_as_photo(self, tmp_path):
        c = _client(tmp_path)
        c.upload_photo = AsyncMock(return_value={"_type": "PHOTO", "photoToken": "t"})
        st = self._file()
        st.is_animated = False
        st.is_video = False
        await _on_topic_media(_update(_message(text=None, sticker=st)), _ctx(c))
        assert c.upload_photo.await_args.kwargs["mimetype"] == "image/webp"

    async def test_animated_sticker_uses_thumbnail(self, tmp_path):
        c = _client(tmp_path)
        c.upload_photo = AsyncMock(return_value={"_type": "PHOTO", "photoToken": "t"})
        st = MagicMock(is_animated=True, is_video=False, thumbnail=self._file())
        await _on_topic_media(_update(_message(text=None, sticker=st)), _ctx(c))
        c.upload_photo.assert_awaited_once()


class TestUnsupported:
    async def test_poll_gets_explanation(self, tmp_path):
        c = _client(tmp_path)
        m = _message(text=None)
        await _on_topic_unsupported(_update(m), _ctx(c))
        m.reply_text.assert_awaited_once()


class TestHandlerOrder:
    def test_edit_handler_registered_before_text_handler(self):
        app = build_tg_app("123:abc", MagicMock(), "-1001", MagicMock())
        callbacks = [h.callback for h in app.handlers[0]]
        assert callbacks.index(_on_topic_edit) < callbacks.index(_on_topic_message)
        names = {c for h in app.handlers[0] for c in getattr(h, "commands", ())}
        assert "rm" in names
