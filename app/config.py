import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    tg_bot_token: str
    # Optional since multi-account mode: MAX accounts and their groups are
    # configured in the bot's private chat. When set, they bootstrap the
    # "main" account so existing .env deployments keep working.
    max_token: str | None = None
    max_device_id: str | None = None
    tg_chat_id: str | None = None
    max_chat_ids: str | None = None
    tg_proxy: str | None = None
    max_proxy: str | None = None
    debug: bool = False
    reply_enabled: bool = True
    state_dir: str = "state"
    tg_allowed_user_id: int | None = None
    sync_executor: bool = False


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_or_exit(name: str) -> int | None:
    raw = os.environ.get(name) or None
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"{name} must be a valid integer, got: {raw!r}")


def load_settings() -> Settings:
    load_dotenv()

    if not os.environ.get("TG_BOT_TOKEN"):
        raise SystemExit(
            "Missing required environment variable: TG_BOT_TOKEN\n"
            "Copy .env.example to .env and set the bot token from @BotFather; "
            "everything else can be configured in the bot's private chat (/start)."
        )

    max_token = os.environ.get("MAX_TOKEN") or None
    max_device_id = os.environ.get("MAX_DEVICE_ID") or None
    if bool(max_token) != bool(max_device_id):
        raise SystemExit("MAX_TOKEN and MAX_DEVICE_ID must be set together (or both left empty).")

    tg_chat_id = _int_or_exit("TG_CHAT_ID")

    return Settings(
        tg_bot_token=os.environ["TG_BOT_TOKEN"],
        max_token=max_token,
        max_device_id=max_device_id,
        tg_chat_id=str(tg_chat_id) if tg_chat_id is not None else None,
        max_chat_ids=os.environ.get("MAX_CHAT_IDS") or None,
        tg_proxy=os.environ.get("TG_PROXY") or None,
        max_proxy=os.environ.get("MAX_PROXY") or None,
        debug=_flag("DEBUG", False),
        reply_enabled=_flag("REPLY_ENABLED", True),
        state_dir=os.environ.get("STATE_DIR") or "state",
        tg_allowed_user_id=_int_or_exit("TG_ALLOWED_USER_ID"),
        sync_executor=_flag("SYNC_EXECUTOR", False),
    )
