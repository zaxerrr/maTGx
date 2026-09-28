import asyncio
import io
import logging
import re

from telegram import Bot, InputFile
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

from app.topics import TopicStore

log = logging.getLogger(__name__)

TG_MAX_LENGTH = 4096
TG_CAPTION_MAX = 1024
TG_TOPIC_NAME_MAX = 128
MAX_RETRIES = 3


# A tag, an HTML entity, or any single character — the atoms we never split.
_HTML_TOKEN_RE = re.compile(r"<[^>]*>|&#?\w+;|.", re.DOTALL)
_TAG_NAME_RE = re.compile(r"</?\s*([a-zA-Z][\w-]*)")


def _tg_len(s: str) -> int:
    """Length as Telegram counts it (UTF-16 code units)."""
    return len(s.encode("utf-16-le")) // 2


def _apply_tag(stack: list, token: str) -> list:
    """Return the open-tag stack after ``token`` (a copy, never mutated)."""
    if not token.startswith("<") or token.endswith("/>"):
        return stack
    m = _TAG_NAME_RE.match(token)
    if not m:
        return stack
    name = m.group(1).lower()
    if token.startswith("</"):
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == name:
                return stack[:i]
        return stack
    return stack + [(name, token)]


def _closers(stack: list) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def split_html(text: str, limit: int) -> list[str]:
    """Split Telegram-HTML ``text`` into chunks of at most ``limit`` UTF-16
    units without cutting through a tag or an entity like ``&amp;``.

    Tags still open at a cut are closed at the end of the chunk and reopened
    at the start of the next one, so every chunk parses on its own. Cuts
    prefer a newline, then a space, in the second half of the chunk.
    """
    if _tg_len(text) <= limit:
        return [text]
    tokens = _HTML_TOKEN_RE.findall(text)
    chunks: list[str] = []
    pos, stack = 0, []
    while pos < len(tokens):
        prefix = "".join(tag for _, tag in stack)
        size = _tg_len(prefix)
        j, st = pos, stack
        best_nl = best_sp = None
        while j < len(tokens):
            tok = tokens[j]
            new_st = _apply_tag(st, tok)
            if size + _tg_len(tok) + _tg_len(_closers(new_st)) > limit:
                break
            size += _tg_len(tok)
            st = new_st
            j += 1
            if tok == "\n":
                best_nl = (j, st)
            elif tok == " ":
                best_sp = (j, st)
        if j == pos:  # a single token longer than the limit — emit it as is
            j, st = pos + 1, _apply_tag(stack, tokens[pos])
        elif j < len(tokens):
            half = pos + (j - pos) // 2
            for best in (best_nl, best_sp):
                if best and best[0] > half:
                    j, st = best
                    break
        body = "".join(tokens[pos:j])
        chunk = (prefix + body + _closers(st)).strip()
        if chunk:
            chunks.append(chunk)
        pos, stack = j, st
    return chunks


def _looks_numeric(title: str) -> bool:
    """A title is 'placeholder' when it carries no human-readable name yet."""
    title = (title or "").strip()
    return not title or title.isdigit() or title.startswith("DM:")


class TelegramSender:
    def __init__(self, token: str, chat_id: str, topic_store: TopicStore,
                 proxy_url: str | None = None):
        if proxy_url:
            request = HTTPXRequest(proxy=proxy_url)
            self._bot = Bot(token=token, request=request)
        else:
            self._bot = Bot(token=token)
        self._chat_id = chat_id
        self._topics = topic_store
        self._topic_lock = asyncio.Lock()

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def topic_store(self) -> TopicStore:
        return self._topics

    async def start(self):
        await self._bot.initialize()
        me = await self._bot.get_me()
        log.info("Telegram bot ready: @%s", me.username)

    async def stop(self):
        await self._bot.shutdown()

    # ── forum topics ───────────────────────────────────────────────

    async def ensure_topic(self, max_chat_id, title: str) -> int | None:
        """Return the Telegram forum topic (thread) ID for a Max chat.

        Creates the topic on first use. If a previously created topic still
        carries a placeholder (numeric) name and a real name is now known,
        the topic is renamed. Returns None if topic creation fails — callers
        then fall back to the General topic.
        """
        title = (title or str(max_chat_id)).strip()[:TG_TOPIC_NAME_MAX]

        existing = self._topics.get_topic(max_chat_id)
        if existing is not None:
            stored = self._topics.get_title(max_chat_id) or ""
            if title and title != stored and _looks_numeric(stored) and not _looks_numeric(title):
                try:
                    await self._bot.edit_forum_topic(
                        chat_id=self._chat_id, message_thread_id=existing, name=title
                    )
                    self._topics.update_title(max_chat_id, title)
                    log.info("Renamed forum topic %s → %r", existing, title)
                except Exception:
                    log.exception("Failed to rename forum topic %s", existing)
            return existing

        async with self._topic_lock:
            existing = self._topics.get_topic(max_chat_id)
            if existing is not None:
                return existing
            try:
                topic = await self._bot.create_forum_topic(
                    chat_id=self._chat_id, name=title
                )
            except Exception:
                log.exception(
                    "Failed to create forum topic for Max chat %s — is the supergroup "
                    "a forum and is the bot an admin with 'Manage Topics'?",
                    max_chat_id,
                )
                return None
            thread_id = topic.message_thread_id
            self._topics.set_topic(max_chat_id, thread_id, title)
            log.info("Created forum topic %r (thread=%s) for Max chat %s",
                     title, thread_id, max_chat_id)
            return thread_id

    # ── helpers ────────────────────────────────────────────────────

    async def _retry(self, coro_factory):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except RetryAfter as e:
                log.warning("Telegram rate limit, retry after %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                log.warning("Telegram timeout (attempt %d/%d)", attempt, MAX_RETRIES)
                await asyncio.sleep(2 * attempt)
            except BadRequest:
                # Malformed request (bad HTML, missing topic, ...) — the same
                # request will fail again, don't waste retries on it.
                log.exception("Telegram rejected the request")
                return None
            except Exception:
                log.exception("Failed to send to Telegram (attempt %d/%d)", attempt, MAX_RETRIES)
                if attempt == MAX_RETRIES:
                    return None
                await asyncio.sleep(2 * attempt)
        log.error("Giving up on Telegram request after %d attempts", MAX_RETRIES)
        return None

    async def _send_with_caption(self, send_fn, caption: str,
                                 message_thread_id: int | None):
        """Send media whose caption may exceed Telegram's 1024 limit: the
        first part goes into the caption, the rest follows as text."""
        parts = split_html(caption, TG_CAPTION_MAX) if caption else [""]
        result = await self._retry(lambda: send_fn(parts[0] or None))
        if result is not None and len(parts) > 1:
            await self.send("\n".join(parts[1:]), message_thread_id=message_thread_id)
        return result

    # ── send methods ───────────────────────────────────────────────

    async def send(self, text: str, message_thread_id: int | None = None) -> None:
        if not text:
            return

        for chunk in split_html(text, TG_MAX_LENGTH):
            await self._retry(
                lambda chunk=chunk: self._bot.send_message(
                    chat_id=self._chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                )
            )

    async def send_photo(self, data: bytes, caption: str = "", filename: str = "photo.jpg",
                         message_thread_id: int | None = None) -> None:
        await self._send_with_caption(
            lambda cap: self._bot.send_photo(
                chat_id=self._chat_id,
                photo=InputFile(io.BytesIO(data), filename=filename),
                caption=cap,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            ),
            caption, message_thread_id,
        )

    async def send_document(self, data: bytes, caption: str = "", filename: str = "file",
                            message_thread_id: int | None = None) -> None:
        await self._send_with_caption(
            lambda cap: self._bot.send_document(
                chat_id=self._chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=cap,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            ),
            caption, message_thread_id,
        )

    async def send_video(self, data: bytes, caption: str = "", filename: str = "video.mp4",
                         message_thread_id: int | None = None) -> None:
        await self._send_with_caption(
            lambda cap: self._bot.send_video(
                chat_id=self._chat_id,
                video=InputFile(io.BytesIO(data), filename=filename),
                caption=cap,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            ),
            caption, message_thread_id,
        )

    async def send_voice(self, data: bytes, caption: str = "",
                         message_thread_id: int | None = None) -> None:
        result = await self._send_with_caption(
            lambda cap: self._bot.send_voice(
                chat_id=self._chat_id,
                voice=InputFile(io.BytesIO(data), filename="voice.ogg"),
                caption=cap,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            ),
            caption, message_thread_id,
        )
        if result is None:
            log.info("send_voice failed, falling back to send_audio")
            await self._send_with_caption(
                lambda cap: self._bot.send_audio(
                    chat_id=self._chat_id,
                    audio=InputFile(io.BytesIO(data), filename="audio.m4a"),
                    caption=cap,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                ),
                caption, message_thread_id,
            )

    async def send_sticker(self, data: bytes, message_thread_id: int | None = None) -> None:
        await self._retry(
            lambda: self._bot.send_sticker(
                chat_id=self._chat_id,
                sticker=InputFile(io.BytesIO(data), filename="sticker.webp"),
                message_thread_id=message_thread_id,
            )
        )
