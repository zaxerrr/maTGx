"""TG → MAX routing in multi-account mode (Bridge in bot_data)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.tg_handler import (
    REGISTRY_KEY,
    _cmd_bind,
    _on_topic_message,
    build_bridge_app,
)
from app.topics import TopicStore


def _rt(tmp_path, acc_id, name, group=-100, prefix=""):
    store = TopicStore(str(tmp_path / f"{acc_id}.json"))
    client = MagicMock()
    client.send_message = AsyncMock(return_value={"message": {"id": "m"}})
    client.msgmap = None
    client.resolver = None
    return SimpleNamespace(account=SimpleNamespace(id=acc_id, title=name),
                           topic_store=store, client=client, group_id=group,
                           sender=SimpleNamespace(title_prefix=prefix))


class FakeBridge:
    def __init__(self, runtimes, owner=1, reply_enabled=True):
        self.runtimes = {r.account.id: r for r in runtimes}
        self.owner_id = owner
        self.reply_enabled = reply_enabled

    def runtimes_for_group(self, gid):
        return [r for r in self.runtimes.values() if r.group_id == gid]

    def resolve_topic(self, gid, thread):
        for r in self.runtimes_for_group(gid):
            if r.topic_store.chat_for_topic(thread) is not None:
                return r
        return None


def _ctx(bridge):
    ctx = MagicMock()
    ctx.bot_data = {REGISTRY_KEY: bridge}
    ctx.bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=77))
    return ctx


def _update(text="hi", thread=5, user=1, chat=-100):
    m = MagicMock()
    m.text, m.entities, m.message_id = text, None, 10
    m.message_thread_id, m.is_topic_message, m.chat_id = thread, True, chat
    m.reply_to_message = None
    m.reply_text = AsyncMock()
    m.set_reaction = AsyncMock()
    u = MagicMock()
    u.message = m
    u.effective_user = SimpleNamespace(id=user)
    return u


class TestRouting:
    async def test_message_goes_to_account_owning_the_topic(self, tmp_path):
        a, b = _rt(tmp_path, "a1", "Work"), _rt(tmp_path, "a2", "Home")
        a.topic_store.set_topic(111, 5, "x")
        b.topic_store.set_topic(222, 6, "y")
        br = FakeBridge([a, b])
        await _on_topic_message(_update(thread=6), _ctx(br))
        b.client.send_message.assert_awaited_once()
        assert b.client.send_message.await_args.args[0] == 222
        a.client.send_message.assert_not_awaited()

    async def test_non_owner_is_ignored(self, tmp_path):
        a = _rt(tmp_path, "a1", "Work")
        a.topic_store.set_topic(111, 5, "x")
        await _on_topic_message(_update(user=2), _ctx(FakeBridge([a])))
        a.client.send_message.assert_not_awaited()

    async def test_no_owner_yet_blocks_sending(self, tmp_path):
        a = _rt(tmp_path, "a1", "Work")
        a.topic_store.set_topic(111, 5, "x")
        await _on_topic_message(_update(), _ctx(FakeBridge([a], owner=None)))
        a.client.send_message.assert_not_awaited()

    async def test_replies_disabled(self, tmp_path):
        a = _rt(tmp_path, "a1", "Work")
        a.topic_store.set_topic(111, 5, "x")
        await _on_topic_message(_update(), _ctx(FakeBridge([a], reply_enabled=False)))
        a.client.send_message.assert_not_awaited()


class TestBind:
    def _bind_update(self, args):
        u = _update(text="/bind " + " ".join(args))
        return u

    async def test_unbound_group_gets_hint(self, tmp_path):
        u = self._bind_update(["123"])
        ctx = _ctx(FakeBridge([]))
        ctx.args = ["123"]
        await _cmd_bind(u, ctx)
        assert "/start" in u.message.reply_text.await_args.args[0]

    async def test_single_account_uses_prefix(self, tmp_path):
        a = _rt(tmp_path, "a1", "Work", prefix="Work · ")
        ctx = _ctx(FakeBridge([a]))
        ctx.args = ["123", "Boss"]
        await _cmd_bind(self._bind_update(ctx.args), ctx)
        assert ctx.bot.create_forum_topic.await_args.kwargs["name"] == "Work · Boss"
        assert a.topic_store.chat_for_topic(77) == 123

    async def test_multiple_accounts_need_selector(self, tmp_path):
        a, b = _rt(tmp_path, "a1", "Work"), _rt(tmp_path, "a2", "Home")
        ctx = _ctx(FakeBridge([a, b]))
        ctx.args = ["123"]
        u = self._bind_update(ctx.args)
        await _cmd_bind(u, ctx)
        assert "несколько аккаунтов" in u.message.reply_text.await_args.args[0]
        ctx.args = ["home", "123"]
        await _cmd_bind(self._bind_update(ctx.args), ctx)
        assert b.topic_store.chat_for_topic(77) == 123
        assert a.topic_store.chat_for_topic(77) is None


def test_bridge_app_has_setup_and_topic_handlers():
    app = build_bridge_app("123:abc")
    all_cmds = {c for grp in app.handlers.values() for h in grp
                for c in getattr(h, "commands", ())}
    assert {"start", "cancel", "bind", "rm", "help"} <= all_cmds
    assert -1 in app.handlers and -2 in app.handlers     # discovery groups
