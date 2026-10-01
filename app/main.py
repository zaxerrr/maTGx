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
from app.accounts import AccountStore
from app.bridge import Bridge
from app.setup_bot import publish_commands
from app.tg_handler import REGISTRY_KEY, build_bridge_app

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
    if settings.max_proxy:
        log.info("Using MAX proxy: %s", settings.max_proxy.split("@")[-1])

    os.makedirs(settings.state_dir, exist_ok=True)
    store = AccountStore(os.path.join(settings.state_dir, "accounts.json"))
    legacy = store.bootstrap_from_env(
        settings.max_token, settings.max_device_id,
        int(settings.tg_chat_id) if settings.tg_chat_id else None,
        settings.max_chat_ids,
    )
    if legacy:
        log.info("Account %r bootstrapped from .env (MAX_TOKEN / TG_CHAT_ID)", legacy.id)

    tg_app = build_bridge_app(settings.tg_bot_token, proxy_url=settings.tg_proxy)
    bridge = Bridge(store, tg_app.bot, settings.state_dir,
                    env_owner_id=settings.tg_allowed_user_id,
                    max_proxy=settings.max_proxy, debug=settings.debug,
                    reply_enabled=settings.reply_enabled)
    tg_app.bot_data[REGISTRY_KEY] = bridge

    await tg_app.initialize()
    me = tg_app.bot.bot
    log.info("Telegram bot ready: @%s", me.username)
    await tg_app.start()
    await publish_commands(tg_app.bot)
    await tg_app.updater.start_polling(drop_pending_updates=True,
                                       allowed_updates=Update.ALL_TYPES)
    if not settings.reply_enabled:
        log.info("REPLY_ENABLED=false: Telegram → MAX sending is off (setup dialog still works)")

    await bridge.start_all()
    if bridge.owner_id is None:
        log.warning("No owner yet: send /start to @%s in a private chat to claim the bridge",
                    me.username)
    if not bridge.runtimes:
        log.info("No MAX account running yet — configure it in the bot's private chat: /start")
        await bridge.notify_owner("MAX-аккаунт не подключён. Откройте меню: /start")

    try:
        await asyncio.Event().wait()   # until SIGTERM/SIGINT cancels main()
    finally:
        log.info("Shutting down...")
        await bridge.stop_all()
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()


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
