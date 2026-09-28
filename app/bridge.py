"""Runtime registry: one running MAX client per configured account.

The Telegram side is a single bot/Application shared by all accounts. Each
account is bound to a forum supergroup; several accounts may share one group
(their topics then get an "<account> · " prefix). Handlers find the account a
Telegram message belongs to by (group id, topic id).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from app.accounts import LEGACY_ID, Account, AccountStore
from app.max_client import MaxClient
from app.max_listener import create_max_client
from app.msgmap import MessageMap
from app.tg_sender import TelegramSender
from app.topics import TopicStore

log = logging.getLogger(__name__)


@dataclass
class AccountRuntime:
    account: Account
    client: MaxClient
    sender: TelegramSender
    topic_store: TopicStore
    msgmap: MessageMap
    task: asyncio.Task | None = None
    status: str = "starting"          # starting | online | auth_failed | stopped
    status_detail: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def group_id(self) -> int | None:
        return self.account.group_id


class Bridge:
    def __init__(self, store: AccountStore, bot: Bot, state_dir: str, *,
                 env_owner_id: int | None = None, max_proxy: str | None = None,
                 debug: bool = False, reply_enabled: bool = True):
        self.store = store
        self.bot = bot
        self.state_dir = state_dir
        self.env_owner_id = env_owner_id
        self.max_proxy = max_proxy
        self.debug = debug
        self.reply_enabled = reply_enabled
        self.runtimes: dict[str, AccountRuntime] = {}
        # Per-user dialog state of the setup wizard (see app/setup_bot.py).
        self.dialogs: dict[int, dict] = {}

    # ── owner ──────────────────────────────────────────────────────

    @property
    def owner_id(self) -> int | None:
        return self.env_owner_id or self.store.owner_id

    async def notify_owner(self, text: str, buttons: list | None = None) -> None:
        if not self.owner_id:
            return
        try:
            await self.bot.send_message(
                chat_id=self.owner_id, text=text, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Could not message the owner")

    # ── lookup ─────────────────────────────────────────────────────

    def runtimes_for_group(self, group_id: int) -> list[AccountRuntime]:
        return [rt for rt in self.runtimes.values() if rt.group_id == int(group_id)]

    def resolve_topic(self, group_id: int, thread_id: int | None) -> AccountRuntime | None:
        """The account whose topic this is, or None."""
        if thread_id is None:
            return None
        for rt in self.runtimes_for_group(group_id):
            if rt.topic_store.chat_for_topic(thread_id) is not None:
                return rt
        return None

    # ── lifecycle ──────────────────────────────────────────────────

    def _paths(self, acc: Account) -> tuple[str, str]:
        # The .env-bootstrapped account keeps the pre-multi-account file names
        # so existing topics and message links survive the upgrade.
        if acc.id == LEGACY_ID:
            base = self.state_dir
        else:
            base = os.path.join(self.state_dir, "accounts", acc.id)
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "topics.json"), os.path.join(base, "messages.db")

    def title_prefix(self, acc: Account) -> str:
        shared = len(self.store.accounts_in_group(acc.group_id)) > 1
        return f"{acc.title} · " if shared else ""

    async def start_all(self) -> None:
        for acc in self.store.accounts():
            await self.start_account(acc.id)

    async def start_account(self, account_id: str) -> AccountRuntime | None:
        acc = self.store.get(account_id)
        if acc is None or not acc.enabled or not acc.has_credentials or acc.group_id is None:
            return None
        await self.stop_account(account_id)

        topics_path, db_path = self._paths(acc)
        topic_store = TopicStore(topics_path)
        msgmap = MessageMap(db_path)
        sender = TelegramSender("", str(acc.group_id), topic_store, bot=self.bot,
                                title_prefix=self.title_prefix(acc))

        async def auth_failed(reason: str, _id=account_id):
            rt = self.runtimes.get(_id)
            if rt:
                rt.status, rt.status_detail = "auth_failed", reason
            a = self.store.get(_id)
            await self.notify_owner(
                f"❌ Аккаунт MAX <b>{_esc(a.title if a else _id)}</b>: авторизация не прошла.\n"
                f"<i>{_esc(reason)}</i>",
                [[InlineKeyboardButton("🔑 Войти заново", callback_data=f"acc:relogin:{_id}")]],
            )

        client = create_max_client(
            acc.token, acc.device_id, sender, acc.max_chat_ids,
            debug=self.debug, msgmap=msgmap, proxy=self.max_proxy,
            on_auth_failed=auth_failed,
        )
        rt = AccountRuntime(acc, client, sender, topic_store, msgmap)

        prev_ready = client._on_ready_cb

        async def ready(snapshot, _rt=rt, _prev=prev_ready):
            _rt.status, _rt.status_detail = "online", ""
            self._adopt_profile_name(_rt.account.id, snapshot)
            if _prev:
                await _prev(snapshot)
        client.on_ready(ready)

        rt.task = asyncio.create_task(client.run(), name=f"max:{account_id}")
        self.runtimes[account_id] = rt
        log.info("Started MAX account %s (%s) → group %s", acc.id, acc.title, acc.group_id)
        return rt

    def _adopt_profile_name(self, account_id: str, snapshot: dict) -> None:
        """Name accounts after their MAX profile unless the owner set a name
        (the .env-bootstrapped account starts as a generic "MAX")."""
        acc = self.store.get(account_id)
        if acc is None or acc.name not in ("", "MAX"):
            return
        from app.login import profile_name
        profile = (snapshot or {}).get("profile") or {}
        name = profile_name(profile) if profile else ""
        if name and name != str(profile.get("id", "")):
            acc.name = name
            self.store.put(acc)

    async def stop_account(self, account_id: str) -> None:
        rt = self.runtimes.pop(account_id, None)
        if rt is None:
            return
        rt.status = "stopped"
        if rt.task:
            rt.task.cancel()
            try:
                await rt.task
            except (asyncio.CancelledError, Exception):
                pass
        rt.msgmap.close()
        log.info("Stopped MAX account %s", account_id)

    async def stop_all(self) -> None:
        for account_id in list(self.runtimes):
            await self.stop_account(account_id)


def _esc(text) -> str:
    from html import escape
    return escape(str(text))
