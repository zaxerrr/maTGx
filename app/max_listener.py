import asyncio
import logging
from datetime import datetime
from html import escape

from app.max_client import MaxClient, MaxMessage
from app.resolver import ContactResolver
from app.msgmap import MessageMap
from app.tg_sender import TG_UPLOAD_LIMIT, TelegramSender

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


# MAX element type → Telegram HTML tag (inverse of tg_handler's mapping).
_MAX_ELEMENT_TAGS = {
    "STRONG": "b",
    "EMPHASIZED": "i",
    "STRIKETHROUGH": "s",
    "UNDERLINE": "u",
    "MONOSPACED": "code",
    "BLOCKQUOTE": "blockquote",
    "QUOTE": "blockquote",
}


def max_text_to_html(text: str, elements) -> str:
    """Render MAX text + formatting ``elements`` ({type, from, length} with
    codepoint offsets) as Telegram HTML. Unknown element types are ignored;
    overlapping ranges are closed and reopened at every boundary so the
    output is always well-formed."""
    text = text or ""
    spans = []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        etype = el.get("type")
        start, length = el.get("from"), el.get("length")
        if not isinstance(start, int) or not isinstance(length, int) or length <= 0:
            continue
        end = min(start + length, len(text))
        if start < 0 or start >= end:
            continue
        if etype == "LINK":
            url = (el.get("attributes") or {}).get("url")
            if not url:
                continue
            open_tag, close_tag = f'<a href="{escape(url, quote=True)}">', "</a>"
        elif etype in _MAX_ELEMENT_TAGS:
            tag = _MAX_ELEMENT_TAGS[etype]
            open_tag, close_tag = f"<{tag}>", f"</{tag}>"
        else:
            continue
        spans.append((start, end, open_tag, close_tag))
    if not spans:
        return escape(text)

    bounds = sorted({0, len(text), *(b for s in spans for b in s[:2])})
    out = []
    for a, b in zip(bounds, bounds[1:]):
        active = [s for s in spans if s[0] <= a and b <= s[1]]
        # Longer spans outside, so nesting stays stable across segments.
        active.sort(key=lambda s: (s[0], -s[1]))
        out.append("".join(s[2] for s in active))
        out.append(escape(text[a:b]))
        out.append("".join(s[3] for s in reversed(active)))
    return "".join(out)


def _header(msg: MaxMessage, sender_label: str, chat_label: str, is_dm: bool) -> str:
    if is_dm:
        return f"✉ <b>{sender_label}</b>"
    return f"💬 <b>{chat_label}</b> | {sender_label}"


def _extract_photo_url(attach: dict) -> str | None:
    """Extract the best available URL for a PHOTO attachment."""
    return attach.get("baseUrl") or attach.get("url")


def _extract_file_url(attach: dict) -> str | None:
    """Extract download URL for a FILE attachment (url field takes priority)."""
    url = attach.get("url")
    if url and url.startswith("http"):
        return url
    return None


def _guess_media_kind(filename: str) -> str:
    name_lower = filename.lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "document"


async def _send_attach(
    attach: dict,
    client: MaxClient,
    sender: TelegramSender,
    header_text: str,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
    reply_to: int | None = None,
):
    """Process and send a single attachment.

    Returns the sent Telegram message (or None if nothing was sent).
    Downloads are capped at the Bot API upload limit so a huge file never
    gets buffered in memory only to be rejected by Telegram.
    """
    atype = attach.get("_type", "")
    log.info("Processing attach _type=%s keys=%s", atype, list(attach.keys()))
    chat_id = msg.chat_id if msg else None
    message_id = msg.message_id if msg else None
    kw = {"message_thread_id": thread_id, "reply_to": reply_to}

    if atype in ("CONTROL", "WIDGET", "INLINE_KEYBOARD"):
        return None

    # MAX's newer client sends voice messages with `_type=UNSUPPORTED` plus an
    # `audioId` + `token`. No download opcode is known for this shape yet
    # (see CLAUDE.md), so show a placeholder instead of probing blindly.
    if atype == "UNSUPPORTED" and attach.get("audioId") is not None:
        duration = attach.get("duration", 0)
        dur_s = f" ({duration // 1000}с)" if duration else ""
        return await sender.send(
            f"{header_text}\n🎙 <i>[голосовое сообщение{dur_s} — "
            "откройте в MAX, скачивание пока не поддерживается]</i>", **kw,
        )

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            log.warning("PHOTO attach has no URL: %s", attach)
            return None
        data = await client.download_file(url, max_bytes=TG_UPLOAD_LIMIT)
        if data:
            return await sender.send_photo(data, caption=header_text, **kw)
        return await sender.send(f"{header_text}\n<i>[фото — не удалось загрузить]</i>", **kw)

    if atype == "VIDEO":
        video_id = attach.get("videoId")
        if video_id is not None and chat_id is not None and message_id:
            url = await client.video_download_url(video_id, chat_id, message_id)
            if url:
                data = await client.download_file(url, max_bytes=TG_UPLOAD_LIMIT)
                if data:
                    return await sender.send_video(data, caption=header_text, **kw)
        note = "<i>[видео — слишком большое или недоступно, превью]</i>"
        thumb = attach.get("thumbnail")
        if thumb:
            data = await client.download_file(thumb, max_bytes=TG_UPLOAD_LIMIT)
            if data:
                return await sender.send_photo(data, caption=f"{header_text}\n{note}", **kw)
        return await sender.send(f"{header_text}\n<i>[видео]</i>", **kw)

    if atype == "FILE":
        name = attach.get("name", "file")
        size = attach.get("size", 0) or 0
        size_str = f" ({_human_size(size)})" if size else ""
        if size > TG_UPLOAD_LIMIT:
            return await sender.send(
                f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}\n"
                f"<i>[слишком большой для Telegram — лимит {_human_size(TG_UPLOAD_LIMIT)}, "
                "откройте в MAX]</i>", **kw,
            )
        url = _extract_file_url(attach)
        if not url and attach.get("fileId") is not None and chat_id is not None and message_id:
            url = await client.file_download_url(attach["fileId"], chat_id, message_id)
        if url:
            data = await client.download_file(url, max_bytes=TG_UPLOAD_LIMIT)
            if data:
                kind = _guess_media_kind(name)
                if kind == "photo":
                    return await sender.send_photo(data, caption=header_text, filename=name, **kw)
                if kind == "video":
                    return await sender.send_video(data, caption=header_text, filename=name, **kw)
                return await sender.send_document(data, caption=header_text, filename=name, **kw)
        return await sender.send(f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}", **kw)

    if atype == "AUDIO":
        url = attach.get("url")
        if url:
            data = await client.download_file(url, max_bytes=TG_UPLOAD_LIMIT)
            if data:
                return await sender.send_voice(data, caption=header_text, **kw)
        return await sender.send(f"{header_text}\n<i>[аудио]</i>", **kw)

    if atype == "STICKER":
        url = attach.get("url")
        if url:
            data = await client.download_file(url, max_bytes=TG_UPLOAD_LIMIT)
            if data:
                return await sender.send_sticker(data, **kw)
        return await sender.send(f"{header_text}\n<i>[стикер]</i>", **kw)

    if atype == "SHARE":
        share_url = attach.get("url", "")
        title = attach.get("title", "")
        desc = attach.get("description", "")
        parts = [header_text]
        if title:
            parts.append(f"🔗 <b>{escape(title)}</b>")
        if share_url:
            parts.append(escape(share_url))
        if desc:
            parts.append(f"<i>{escape(desc[:200])}</i>")
        return await sender.send("\n".join(parts), **kw)

    if atype == "LOCATION":
        lat = attach.get("lat") or attach.get("latitude")
        lon = attach.get("lon") or attach.get("lng") or attach.get("longitude")
        if lat and lon:
            return await sender.send(f"{header_text}\n📍 {lat}, {lon}", **kw)
        return await sender.send(f"{header_text}\n<i>[геолокация]</i>", **kw)

    if atype == "CONTACT":
        name = attach.get("name", "")
        phone = attach.get("phone", "")
        text = f"{header_text}\n👤 {escape(name)}"
        if phone:
            text += f" — {escape(phone)}"
        return await sender.send(text, **kw)

    log.info("Unknown attach type %s, sending as info", atype)
    return await sender.send(f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>", **kw)


def _meaningful(attaches) -> list:
    return [
        a for a in attaches or []
        if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
    ]


async def _send_body(text: str, attaches: list, header: str, client: MaxClient,
                     sender: TelegramSender, thread_id: int | None,
                     msg: MaxMessage | None, reply_to: int | None = None,
                     elements=None):
    """Send text + attachments; the text rides as the first caption.
    Returns the first Telegram message sent."""
    first = None
    body_html = max_text_to_html(text, elements)
    attaches = _meaningful(attaches)
    if attaches:
        text_sent = False
        for i, attach in enumerate(attaches):
            if i == 0 and text:
                cap = f"{header}\n{body_html}"
                text_sent = True
            else:
                cap = header
            sent = await _send_attach(attach, client, sender, cap, thread_id=thread_id,
                                      msg=msg, reply_to=reply_to if first is None else None)
            log.info("Forwarded attach _type=%s → TG", attach.get("_type"))
            first = first or sent
        if text and not text_sent:
            sent = await sender.send(f"{header}\n{body_html}", message_thread_id=thread_id)
            first = first or sent
    else:
        body = body_html if text else "<i>[нетекстовое сообщение]</i>"
        first = await sender.send(f"{header}\n{body}", message_thread_id=thread_id,
                                  reply_to=reply_to)
    return first


async def _handle_linked_message(
    link: dict,
    link_type: str,
    header_text: str,
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
):
    """Render a FORWARD or REPLY link inside a message (quoted form).
    Returns the first Telegram message sent."""
    inner = link.get("message") or link
    fwd_sender_id = inner.get("sender") or link.get("sender")
    fwd_text = inner.get("text", "") or link.get("text", "")
    fwd_attaches = inner.get("attaches") or link.get("attaches") or []
    fwd_elements = inner.get("elements") or link.get("elements") or []

    fwd_sender_label = ""
    if fwd_sender_id:
        fwd_sender_label = escape(await resolver.resolve_user(fwd_sender_id))

    if link_type == "FORWARD":
        prefix = "↩️ <b>Переслано</b>"
        if fwd_sender_label:
            prefix = f"↩️ <b>Переслано от {fwd_sender_label}</b>"
    else:
        prefix = "↩ <b>Ответ</b>"
        if fwd_sender_label:
            prefix = f"↩ <b>Ответ на {fwd_sender_label}</b>"

    full_header = f"{header_text}\n{prefix}"
    if not fwd_text and not _meaningful(fwd_attaches):
        return await sender.send(f"{full_header}\n<i>[без содержимого]</i>",
                                 message_thread_id=thread_id)
    return await _send_body(fwd_text, fwd_attaches, full_header, client, sender,
                            thread_id, msg, elements=fwd_elements)


def _linked_message_id(link: dict):
    inner = link.get("message") if isinstance(link.get("message"), dict) else {}
    mid = link.get("messageId") or inner.get("id")
    return str(mid) if mid is not None else None


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def create_max_client(
    max_token: str, max_device_id: str, sender: TelegramSender, max_chat_ids: str | None = None,
    debug: bool = False, msgmap: MessageMap | None = None,
) -> MaxClient:
    client = MaxClient(token=max_token, device_id=max_device_id, debug=debug, chat_ids=max_chat_ids)
    resolver = ContactResolver(client=client)
    # Expose for tg_handler commands like /profile, replies and edits.
    client.resolver = resolver
    client.msgmap = msgmap

    _first_connect = True
    _notif_count = 0
    _last_notif_time: datetime | None = None
    _auth_alert_sent = False
    _disconnect_alerted = False
    _chat_locks: dict = {}

    def _can_notify() -> bool:
        if _last_notif_time is None:
            return True
        elapsed = (datetime.now() - _last_notif_time).total_seconds()
        if _notif_count == 1:
            return elapsed >= 3600    # 2-е: через 1 час
        if _notif_count == 2:
            return elapsed >= 10800   # 3-е: через 3 часа
        return elapsed >= 86400       # 4-е и далее: раз в сутки

    async def _post_intro(max_chat_id, thread_id):
        from app.tg_handler import post_topic_intro
        await post_topic_intro(sender.bot, sender.chat_id, client, max_chat_id, thread_id)

    sender.on_topic_recreated = _post_intro

    @client.on_ready
    async def handle_ready(snapshot: dict):
        nonlocal _first_connect, _auth_alert_sent, _disconnect_alerted
        _auth_alert_sent = False
        participant_ids = resolver.load_snapshot(snapshot)

        if participant_ids:
            log.info("Batch-resolving %d participants...", len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info("Resolved %d users, %d chats known",
                     len(resolver.users), len(resolver.chats))
            log.debug("Known chats: %s", resolver.chats)
            log.debug("Known users: %s", resolver.users)

        if _first_connect:
            chat_count = len(resolver.chats)
            await sender.send(f"✅ <b>Max:</b> подключён | чатов: {chat_count}")
        elif _disconnect_alerted:
            # Only close the loop on an alert the user actually saw.
            await sender.send("✅ <b>Max:</b> соединение восстановлено")
        _first_connect = False
        _disconnect_alerted = False

    @client.on_auth_failed
    async def handle_auth_failed(reason: str):
        # One alert per failure streak — reconnect attempts keep failing the
        # same way until the token is replaced, no point repeating it.
        nonlocal _auth_alert_sent
        if _auth_alert_sent:
            return
        _auth_alert_sent = True
        await sender.send(
            "❌ <b>Max:</b> авторизация не прошла — "
            f"{escape(reason)}.\n"
            "Скорее всего, токен устарел (вход в web.max.ru с другого устройства). "
            "Обновите <code>MAX_TOKEN</code> в <code>.env</code> и перезапустите мост."
        )

    @client.on_disconnect
    async def handle_disconnect():
        nonlocal _notif_count, _last_notif_time, _disconnect_alerted
        if not _can_notify():
            log.info("Disconnect notification suppressed (throttle)")
            return
        _notif_count += 1
        _last_notif_time = datetime.now()
        _disconnect_alerted = True
        await sender.send("⚠️ <b>Max:</b> соединение потеряно, переподключение...")

    @client.on_message
    async def handle_message(msg: MaxMessage):
        # Messages of one chat are processed strictly one after another (the
        # lock is FIFO and tasks start in arrival order), so a slow photo
        # download can't let the next text overtake it, and a new chat's
        # topic + intro card are created exactly once.
        lock = _chat_locks.setdefault(msg.chat_id, asyncio.Lock())
        async with lock:
            await _process_message(msg)

    async def _process_message(msg: MaxMessage):
        log.info(
            "New message: chat=%s sender=%s is_self=%s text_len=%d attaches=%d",
            msg.chat_id,
            msg.sender_id,
            msg.is_self,
            len(msg.text),
            len(msg.attaches),
        )

        if msg.is_self:
            return

        # Already bridged? Then this is a redelivery or an edit.
        if msgmap is not None and msg.message_id:
            known_tg = msgmap.tg_for_max(msg.chat_id, msg.message_id)
            if known_tg is not None:
                await _handle_edit(msg, known_tg)
                return

        raw_sender = await resolver.resolve_user(msg.sender_id)
        is_dm = resolver.is_dm(msg.chat_id)
        raw_chat = resolver.chat_name(msg.chat_id)

        # One forum topic per Max chat. Prefer a human title:
        # - DMs → the peer's name
        # - Groups with a known title → the chat title
        # - Chats discovered at runtime (no known title yet) → the sender's name
        #   (better than the numeric chat ID; ensure_topic will rename later if a
        #   real chat title appears).
        chat_title_known = raw_chat != str(msg.chat_id) and not raw_chat.startswith("DM:")
        if is_dm or not chat_title_known:
            topic_title = raw_sender
        else:
            topic_title = raw_chat

        existing_thread = sender.topic_store.get_topic(msg.chat_id)
        thread_id = await sender.ensure_topic(msg.chat_id, topic_title)

        # First time we touch this chat → publish a pinned profile card so the
        # topic starts with context (avatar, name, etc.).
        if existing_thread is None and thread_id is not None:
            await _post_intro(msg.chat_id, thread_id)

        header_text = _header(msg, escape(raw_sender), escape(raw_chat), is_dm)

        link = msg.link
        link_type = link.get("type") if isinstance(link, dict) else None

        first = None
        if link_type == "REPLY" and msgmap is not None:
            replied = _linked_message_id(link)
            tg_reply = msgmap.tg_for_max(msg.chat_id, replied) if replied else None
            if tg_reply is not None:
                # The replied-to message is in the topic: use a native reply.
                first = await _send_body(msg.text, msg.attaches, header_text, client,
                                         sender, thread_id, msg, reply_to=tg_reply,
                                         elements=msg.elements)
                _remember(msg, first)
                return

        if link_type in ("FORWARD", "REPLY"):
            first = await _handle_linked_message(link, link_type, header_text, client,
                                                 sender, resolver, thread_id=thread_id, msg=msg)
            if msg.text or _meaningful(msg.attaches):
                sent = await _send_body(msg.text, msg.attaches, header_text, client,
                                        sender, thread_id, msg, elements=msg.elements)
                first = sent or first
            log.info("Forwarded link type=%s → TG", link_type)
        else:
            first = await _send_body(msg.text, msg.attaches, header_text, client,
                                     sender, thread_id, msg, elements=msg.elements)
            log.info("Forwarded message → TG")
        _remember(msg, first)

    def _remember(msg: MaxMessage, tg_message) -> None:
        if msgmap is not None and tg_message is not None and msg.message_id:
            msgmap.add(msg.chat_id, msg.message_id, tg_message.message_id, msg.text)

    async def _handle_edit(msg: MaxMessage, tg_msg_id: int) -> None:
        old_text = msgmap.text_for_max(msg.chat_id, msg.message_id) or ""
        if (msg.text or "") == old_text:
            log.info("Duplicate delivery of MAX message %s — skipped", msg.message_id)
            return
        raw_sender = await resolver.resolve_user(msg.sender_id)
        header = _header(msg, escape(raw_sender), escape(resolver.chat_name(msg.chat_id)),
                         resolver.is_dm(msg.chat_id))
        body = max_text_to_html(msg.text, msg.elements) if msg.text else "<i>[текст удалён]</i>"
        html = f"{header}\n{body}\n<i>✏️ изменено</i>"
        if not await sender.edit_text(tg_msg_id, html):
            await sender.send(f"✏️ <b>Изменено:</b>\n{body}",
                              message_thread_id=sender.topic_store.get_topic(msg.chat_id),
                              reply_to=tg_msg_id)
        msgmap.set_text(msg.chat_id, msg.message_id, msg.text or "")
        log.info("Mirrored edit of MAX message %s → TG %s", msg.message_id, tg_msg_id)

    return client
