"""Persistent registry of MAX accounts, the bot owner and known Telegram groups.

Stored in ``state/accounts.json`` (chmod 600 — it holds MAX tokens). Written
atomically. Everything the setup dialog in the bot's private chat changes
lives here, so the only thing .env must provide is TG_BOT_TOKEN.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields

log = logging.getLogger(__name__)

LEGACY_ID = "main"   # account bootstrapped from MAX_TOKEN/TG_CHAT_ID in .env


@dataclass
class Account:
    id: str
    name: str = ""
    token: str = ""
    device_id: str = ""
    group_id: int | None = None
    phone: str = ""
    max_chat_ids: str | None = None
    enabled: bool = True
    env_fingerprint: str = ""   # hash of the .env token this account came from

    @property
    def has_credentials(self) -> bool:
        return bool(self.token and self.device_id)

    @property
    def title(self) -> str:
        return self.name or self.phone or self.id

    @classmethod
    def from_dict(cls, data: dict) -> "Account":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class GroupInfo:
    id: int
    title: str = ""
    is_forum: bool | None = None
    can_manage_topics: bool | None = None

    @property
    def ready(self) -> bool:
        return bool(self.is_forum and self.can_manage_topics)


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else ""


@dataclass
class _State:
    owner_id: int | None = None
    accounts: dict[str, Account] = field(default_factory=dict)
    groups: dict[int, GroupInfo] = field(default_factory=dict)


class AccountStore:
    def __init__(self, path: str):
        self._path = path
        self._s = _State()
        self._load()

    # ── persistence ────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            self._s.owner_id = data.get("owner_id")
            for a in data.get("accounts", []):
                acc = Account.from_dict(a)
                self._s.accounts[acc.id] = acc
            for g in data.get("groups", []):
                info = GroupInfo(**{k: g.get(k) for k in ("id", "title", "is_forum",
                                                          "can_manage_topics")})
                self._s.groups[int(info.id)] = info
        except Exception:
            log.exception("Failed to load %s — starting empty", self._path)
            self._s = _State()

    def save(self) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        data = {
            "owner_id": self._s.owner_id,
            "accounts": [asdict(a) for a in self._s.accounts.values()],
            "groups": [asdict(g) for g in self._s.groups.values()],
        }
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except Exception:
            log.exception("Failed to save %s", self._path)
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ── owner ──────────────────────────────────────────────────────

    @property
    def owner_id(self) -> int | None:
        return self._s.owner_id

    def set_owner(self, user_id: int) -> None:
        self._s.owner_id = int(user_id)
        self.save()

    # ── accounts ───────────────────────────────────────────────────

    def accounts(self) -> list[Account]:
        return list(self._s.accounts.values())

    def get(self, account_id: str) -> Account | None:
        return self._s.accounts.get(account_id)

    def new_id(self) -> str:
        n = 1
        while f"a{n}" in self._s.accounts:
            n += 1
        return f"a{n}"

    def put(self, account: Account) -> Account:
        self._s.accounts[account.id] = account
        self.save()
        return account

    def remove(self, account_id: str) -> Account | None:
        acc = self._s.accounts.pop(account_id, None)
        if acc:
            self.save()
        return acc

    def accounts_in_group(self, group_id: int) -> list[Account]:
        return [a for a in self._s.accounts.values() if a.group_id == group_id]

    def bootstrap_from_env(self, token: str | None, device_id: str | None,
                           group_id: int | None, max_chat_ids: str | None) -> Account | None:
        """Keep the pre-multi-account .env setup working.

        Creates the ``main`` account from MAX_TOKEN / MAX_DEVICE_ID /
        TG_CHAT_ID on first start. Later, a *changed* token in .env wins
        (the user edited it on purpose), while a token obtained through the
        bot dialog is not overwritten by the same old .env value.
        """
        if not (token and device_id) and group_id is None:
            return None
        acc = self._s.accounts.get(LEGACY_ID)
        fp = fingerprint(token or "")
        changed = False
        if acc is None:
            acc = Account(id=LEGACY_ID, name="MAX")
            changed = True
        if token and device_id and fp != acc.env_fingerprint:
            acc.token, acc.device_id, acc.env_fingerprint = token, device_id, fp
            changed = True
        if group_id is not None and acc.group_id is None:
            acc.group_id = group_id
            changed = True
        if max_chat_ids and acc.max_chat_ids != max_chat_ids:
            acc.max_chat_ids = max_chat_ids
            changed = True
        if changed:
            self.put(acc)
        return acc

    # ── groups ─────────────────────────────────────────────────────

    def groups(self) -> list[GroupInfo]:
        return list(self._s.groups.values())

    def get_group(self, group_id: int) -> GroupInfo | None:
        return self._s.groups.get(int(group_id))

    def remember_group(self, group_id: int, title: str | None = None,
                       is_forum: bool | None = None,
                       can_manage_topics: bool | None = None) -> GroupInfo:
        existing = self._s.groups.get(int(group_id))
        g = existing or GroupInfo(id=int(group_id))
        before = asdict(g)
        if title:
            g.title = title
        if is_forum is not None:
            g.is_forum = is_forum
        if can_manage_topics is not None:
            g.can_manage_topics = can_manage_topics
        self._s.groups[int(group_id)] = g
        if existing is None or asdict(g) != before:
            self.save()
        return g

    def forget_group(self, group_id: int) -> None:
        if self._s.groups.pop(int(group_id), None):
            self.save()
