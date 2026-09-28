import asyncio
import concurrent.futures
import logging
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

from telegram import Update

from app.config import load_settings
from app.max_listener import create_max_client
from app.msgmap import MessageMap
from app.tg_handler import build_tg_app
from app.tg_sender import TelegramSender
from app.topics import TopicStore

threading.stack_size(524288)

log = logging.getLogger("max2tg")


class _SyncExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor that runs callables synchronously without spawning threads.

    Python 3.12 requires set_default_executor() to receive a ThreadPoolExecutor,
    so we subclass it and override submit() to bypass _adjust_thread_count().
    Opt-in (SYNC_EXECUTOR=true) for low-resource servers where the OS cannot
    create new threads. DNS resolution (getaddrinfo) then runs on the event
    loop and a slow resolver stalls the whole bridge, heartbeats included —
    so it is off by default.
    """

    def submit(self, fn, /, *args, **kwargs):
        f: concurrent.futures.Future = concurrent.futures.Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as exc:
            f.set_exception(exc)
        return f


async def main():
    settings = load_settings()

    loop = asyncio.get_running_loop()
    if settings.sync_executor:
        loop.set_default_executor(_SyncExecutor())
    else:
        loop.set_default_executor(ThreadPoolExecutor(max_workers=4))

    level = logging.DEBUG if settings.debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    log_dir = os.environ.get("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "max2tg.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    logging.basicConfig(level=level, handlers=[console_handler, file_handler], force=True)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING if not settings.debug else logging.DEBUG)

    log.info("Debug mode: %s", "ON" if settings.debug else "OFF")
    if settings.sync_executor:
        log.info("SYNC_EXECUTOR on: blocking calls run on the event loop")

    if settings.tg_proxy:
        log.info("Using Telegram proxy: %s", settings.tg_proxy.split("@")[-1])

    os.makedirs(settings.state_dir, exist_ok=True)
    topic_store = TopicStore(os.path.join(settings.state_dir, "topics.json"))
    msgmap = MessageMap(os.path.join(settings.state_dir, "messages.db"))

    sender = TelegramSender(settings.tg_bot_token, settings.tg_chat_id, topic_store,
                            proxy_url=settings.tg_proxy)
    await sender.start()

    client = create_max_client(
        settings.max_token, settings.max_device_id, sender, settings.max_chat_ids,
        debug=settings.debug, msgmap=msgmap,
    )

    tg_app = None
    if settings.reply_enabled and settings.tg_allowed_user_id is None:
        log.warning(
            "TG_ALLOWED_USER_ID is not set: ANY member of the supergroup can "
            "send messages to MAX on your behalf. Set it in .env."
        )
    if settings.reply_enabled:
        tg_app = build_tg_app(settings.tg_bot_token, client, settings.tg_chat_id,
                              topic_store, allowed_user_id=settings.tg_allowed_user_id,
                              proxy_url=settings.tg_proxy)
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info("Telegram polling started (reply → Max enabled)")
    else:
        log.info("Reply to Max disabled (REPLY_ENABLED=false)")

    log.info("Starting Max listener...")
    try:
        await client.run()
    finally:
        log.info("Shutting down...")
        if tg_app:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()
        await sender.stop()
        msgmap.close()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    main_task = loop.create_task(main())
    # `docker stop` sends SIGTERM: cancel main() so its finally-block closes
    # Telegram polling, the bot session and the message DB cleanly.
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, main_task.cancel)
        except (NotImplementedError, RuntimeError):
            pass  # e.g. Windows
    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Stopped.")
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
