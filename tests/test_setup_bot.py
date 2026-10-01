"""Tests for app/setup_bot.py — private-chat setup dialog and group discovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.constants import ChatMemberStatus, ChatType

from app import setup_bot as sb
from app.accounts import Account, AccountStore
from app.bridge import Bridge
from app.login import CodeRejected, LoginError


def _bridge(tmp_path, owner=None):
    store = AccountStore(str(tmp_path / "accounts.json"))
    bot = MagicMock()
    bot.send_message = AsyncMock()
    br = Bridge(store, bot, str(tmp_path), env_owner_id=owner)
    br.start_account = AsyncMock()
    br.stop_account = AsyncMock()
    return br


def _ctx(br, is_forum=True, can_manage=True, status=ChatMemberStatus.ADMINISTRATOR):
    ctx = MagicMock()
    ctx.bot_data = {sb.REGISTRY_KEY: br}
    ctx.bot.id = 999
    ctx.bot.get_chat = AsyncMock(side_effect=lambda gid: SimpleNamespace(
        id=gid, title=f"Group{gid}", is_forum=is_forum))
    ctx.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status=status, can_manage_topics=can_manage))
    return ctx


def _msg(text=""):
    m = MagicMock()
    m.text = text
    m.reply_text = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    m.delete = AsyncMock()
    return m


def _update(user_id=1, text="", data=None):
    u = MagicMock()
    u.effective_user = SimpleNamespace(id=user_id, username="u")
    u.effective_message = _msg(text)
    u.callback_query = None
    if data is not None:
        q = MagicMock()
        q.data = data
        q.answer = AsyncMock()
        q.edit_message_text = AsyncMock()
        q.message = _msg()
        u.callback_query = q
    return u


def _last_text(update):
    q = getattr(update, "callback_query", None)
    if q is not None and q.edit_message_text.await_count:
        return q.edit_message_text.await_args.args[0]
    return update.effective_message.reply_text.await_args.args[0]


class TestOwner:
    async def test_first_start_claims_owner_and_shows_login(self, tmp_path):
        br = _bridge(tmp_path)
        u = _update(user_id=5)
        await sb._cmd_start(u, _ctx(br))
        assert br.owner_id == 5
        kb = u.effective_message.reply_text.await_args.kwargs["reply_markup"]
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert datas == ["acc:phone:a1", "acc:token:a1"]

    async def test_stranger_is_refused(self, tmp_path):
        br = _bridge(tmp_path)
        br.store.set_owner(5)
        u = _update(user_id=6)
        await sb._cmd_start(u, _ctx(br))
        assert "другим пользователем" in _last_text(u)

    async def test_env_owner_cannot_be_claimed_by_others(self, tmp_path):
        br = _bridge(tmp_path, owner=5)
        u = _update(user_id=6)
        await sb._cmd_start(u, _ctx(br))
        assert br.store.owner_id is None


class TestPhoneLogin:
    async def test_full_phone_flow_then_group_choice(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.remember_group(-100, "Chats", True, True)
        ctx = _ctx(br)
        await sb._on_setup_button(_update(data="acc:phone:a1"), ctx)
        assert br.dialogs[1]["step"] == "phone"

        fake = MagicMock()
        fake.start = AsyncMock(return_value="+79991234567")
        fake.finish = AsyncMock(side_effect=[
            CodeRejected("Неверный код"),
            ("TOKEN", "DEV", {"id": 7, "names": [{"firstName": "Ann"}]}),
        ])
        fake.close = AsyncMock()
        with patch.object(sb, "PhoneLogin", return_value=fake):
            await sb._on_private_text(_update(text="8 999 123 45 67"), ctx)
        assert br.dialogs[1]["step"] == "code"

        wrong = _update(text="000000")
        await sb._on_private_text(wrong, ctx)
        wrong.effective_message.delete.assert_awaited()          # code wiped
        assert "ещё раз" in _last_text(wrong)

        right = _update(text="123456")
        await sb._on_private_text(right, ctx)
        acc = br.store.get("a1")
        assert (acc.token, acc.device_id, acc.name, acc.phone) == \
            ("TOKEN", "DEV", "Ann", "+79991234567")
        assert 1 not in br.dialogs
        # No group yet → the group picker is offered with the known group.
        kb = right.effective_message.reply_text.await_args.kwargs["reply_markup"]
        assert "grp:set:a1:-100" in [b.callback_data for r in kb.inline_keyboard for b in r]

    async def test_code_attempts_run_out(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        login = MagicMock(finish=AsyncMock(side_effect=CodeRejected("bad")), close=AsyncMock())
        sb._start_dialog(br, 1, step="code", acc_id="a1", login=login, attempts=0)
        for _ in range(sb.MAX_CODE_ATTEMPTS):
            u = _update(text="1")
            await sb._on_private_text(u, _ctx(br))
        assert "Попытки кончились" in _last_text(u)
        assert 1 not in br.dialogs
        login.close.assert_awaited()

    async def test_phone_rejected_by_max(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        sb._start_dialog(br, 1, step="phone", acc_id="a1")
        fake = MagicMock(start=AsyncMock(side_effect=LoginError("Проверка не пройдена")))
        with patch.object(sb, "PhoneLogin", return_value=fake):
            u = _update(text="+70000000000")
            await sb._on_private_text(u, _ctx(br))
        assert "Проверка не пройдена" in _last_text(u)
        assert br.dialogs[1]["step"] == "phone"

    async def test_existing_group_restarts_account(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", group_id=-100))
        login = MagicMock(finish=AsyncMock(return_value=("T", "D", {"id": 1})),
                          close=AsyncMock())
        sb._start_dialog(br, 1, step="code", acc_id="a1", login=login)
        await sb._on_private_text(_update(text="123456"), _ctx(br))
        br.start_account.assert_awaited_with("a1")

    async def test_dialog_expires(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        sb._start_dialog(br, 1, step="phone", acc_id="a1")
        br.dialogs[1]["expires"] = 0
        u = _update(text="+79991234567")
        await sb._on_private_text(u, _ctx(br))
        assert "истекло" in _last_text(u)


class TestTokenLogin:
    @pytest.mark.parametrize("text,expected", [
        ('{"token":"An_tok123456789012345","viewerId":1}', ("An_tok123456789012345", None)),
        ('"An_tok123456789012345"', ("An_tok123456789012345", None)),
        ("An_tok123456789012345 0c9e-11ab-22cd-33ef-44aa", ("An_tok123456789012345", "0c9e-11ab-22cd-33ef-44aa")),
        ("short", (None, None)),
        ("{broken", (None, None)),
    ])
    def test_parse_token_input(self, text, expected):
        assert sb.parse_token_input(text) == expected

    async def test_token_then_device_is_verified_and_saved(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        await sb._on_setup_button(_update(data="acc:token:a1"), ctx)
        tok = _update(text='{"token":"An_secret_token_value_123"}')
        await sb._on_private_text(tok, ctx)
        tok.effective_message.delete.assert_awaited()            # secret wiped
        assert br.dialogs[1]["step"] == "device"

        dev = _update(text="3f2b8c1e-0000-4a4a-9b9b-123456789abc")
        with patch.object(sb, "check_token", AsyncMock(return_value={"id": 3, "names": [{"name": "Bob"}]})) as ct:
            await sb._on_private_text(dev, ctx)
        ct.assert_awaited_once_with("An_secret_token_value_123",
                                    "3f2b8c1e-0000-4a4a-9b9b-123456789abc", None)
        dev.effective_message.delete.assert_awaited()
        assert br.store.get("a1").name == "Bob"

    async def test_rejected_token_not_saved(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        sb._start_dialog(br, 1, step="token", acc_id="a1")
        u = _update(text="An_secret_token_value_123 3f2b8c1e-0000-4a4a-9b9b-123456789abc")
        with patch.object(sb, "check_token", AsyncMock(side_effect=LoginError("Invalid token"))):
            await sb._on_private_text(u, _ctx(br))
        assert br.store.get("a1") is None
        note = u.effective_message.reply_text.return_value
        assert "не принял" in note.edit_text.await_args.args[0]


class TestGroups:
    async def test_bind_ready_group_starts_account(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", name="Ann", token="t", device_id="d"))
        u = _update(data="grp:set:a1:-100")
        await sb._on_setup_button(u, _ctx(br))
        assert br.store.get("a1").group_id == -100
        br.start_account.assert_awaited_with("a1")
        assert "привязан" in _last_text(u)

    async def test_bind_unready_group_refused(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", token="t", device_id="d"))
        u = _update(data="grp:set:a1:-100")
        await sb._on_setup_button(u, _ctx(br, is_forum=False))
        assert br.store.get("a1").group_id is None
        assert "не подходит" in _last_text(u)

    async def test_bot_added_to_ready_group_offers_binding(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", name="Ann", token="t", device_id="d"))
        u = MagicMock()
        u.my_chat_member = SimpleNamespace(
            chat=SimpleNamespace(id=-100, title="Chats", type=ChatType.SUPERGROUP, is_forum=True),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR,
                                            can_manage_topics=True))
        ctx = _ctx(br)
        await sb._on_my_chat_member(u, ctx)
        assert br.store.get_group(-100).ready
        ctx.bot.get_chat.assert_not_awaited()          # data taken from the update
        kw = br.bot.send_message.await_args.kwargs
        assert "готова" in kw["text"]
        datas = [b.callback_data for r in kw["reply_markup"].inline_keyboard for b in r]
        assert "grp:set:a1:-100" in datas

    async def test_bot_added_without_rights_explains(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        u = MagicMock()
        u.my_chat_member = SimpleNamespace(
            chat=SimpleNamespace(id=-100, title="Chats", type=ChatType.SUPERGROUP, is_forum=True),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.MEMBER))
        await sb._on_my_chat_member(u, _ctx(br))
        assert "Управление темами" in br.bot.send_message.await_args.kwargs["text"]

    async def test_bot_removed_stops_accounts(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", token="t", device_id="d", group_id=-100))
        br.store.remember_group(-100, "Chats", True, True)
        u = MagicMock()
        u.my_chat_member = SimpleNamespace(
            chat=SimpleNamespace(id=-100, title="Chats", type=ChatType.SUPERGROUP),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.LEFT))
        await sb._on_my_chat_member(u, _ctx(br))
        br.stop_account.assert_awaited_with("a1")
        assert br.store.get_group(-100) is None

    async def test_migration_moves_accounts(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", token="t", device_id="d", group_id=-5))
        u = MagicMock()
        u.effective_message = SimpleNamespace(chat_id=-5, migrate_to_chat_id=-1005)
        await sb._on_migrate(u, _ctx(br))
        assert br.store.get("a1").group_id == -1005
        br.start_account.assert_awaited_with("a1")

    async def test_group_seen_is_remembered_once(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        u = MagicMock()
        u.effective_chat = SimpleNamespace(id=-7, type=ChatType.SUPERGROUP)
        await sb._on_group_seen(u, ctx)
        await sb._on_group_seen(u, ctx)
        assert br.store.get_group(-7).title == "Group-7"
        assert ctx.bot.get_chat.await_count == 1


class TestProbe:
    async def test_get_chat_failure_keeps_member_check(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        ctx.bot.get_chat = AsyncMock(side_effect=TypeError("unparseable field"))
        g = await sb.probe_group(ctx.bot, br, -100)
        assert g.can_manage_topics is True and g.is_forum is None


class TestMenu:
    async def test_menu_lists_accounts_with_status(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", name="Ann", token="t", device_id="d"))
        br.store.put(Account(id="a2", name="Bob"))
        text, kb = sb.render_menu(br)
        assert "Ann" in text and "не выбрана группа" in text
        assert "Bob" in text and "нет токена" in text

    async def test_delete_account(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", name="Ann"))
        await sb._on_setup_button(_update(data="acc:delok:a1"), _ctx(br))
        br.stop_account.assert_awaited_with("a1")
        assert br.store.get("a1") is None


class TestNoNeedlessRestarts:
    async def test_binding_to_separate_group_does_not_restart_others(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", token="t", device_id="d", group_id=-200))
        br.store.put(Account(id="a2", token="t", device_id="d"))
        br.runtimes = {"a1": object()}
        await sb._on_setup_button(_update(data="grp:set:a2:-100"), _ctx(br))
        assert [c.args[0] for c in br.start_account.await_args_list] == ["a2"]

    async def test_sharing_a_group_restarts_neighbour_for_prefix(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", name="Work", token="t", device_id="d", group_id=-100))
        br.store.put(Account(id="a2", name="Home", token="t", device_id="d"))
        br.runtimes = {"a1": object()}
        await sb._on_setup_button(_update(data="grp:set:a2:-100"), _ctx(br))
        assert [c.args[0] for c in br.start_account.await_args_list] == ["a2", "a1"]

    async def test_rights_update_does_not_restart_running_account(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", token="t", device_id="d", group_id=-100))
        br.runtimes = {"a1": object()}
        u = MagicMock()
        u.my_chat_member = SimpleNamespace(
            chat=SimpleNamespace(id=-100, title="Chats", type=ChatType.SUPERGROUP, is_forum=True),
            new_chat_member=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR,
                                            can_manage_topics=True))
        await sb._on_my_chat_member(u, _ctx(br))
        br.start_account.assert_not_awaited()


class TestLoginCommand:
    def _fake_login(self):
        fake = MagicMock()
        fake.start = AsyncMock(return_value="+79991234567")
        fake.close = AsyncMock()
        return fake

    async def test_login_with_phone_requests_code_immediately(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        ctx.args = ["+7", "999", "123-45-67"]
        u = _update(text="/login +7 999 123-45-67")
        fake = self._fake_login()
        with patch.object(sb, "PhoneLogin", return_value=fake):
            await sb._cmd_login(u, ctx)
        fake.start.assert_awaited_once_with("+7 999 123-45-67")
        assert br.dialogs[1]["step"] == "code" and br.dialogs[1]["acc_id"] == "a1"
        assert "Код отправлен" in _last_text(u)

    async def test_login_without_phone_asks_for_it(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        ctx.args = []
        u = _update(text="/login")
        await sb._cmd_login(u, ctx)
        assert br.dialogs[1]["step"] == "phone"
        assert "номер" in _last_text(u)

    async def test_single_account_is_relogged(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="main", name="Ann", token="old", device_id="d", group_id=-1))
        ctx = _ctx(br)
        ctx.args = []
        await sb._cmd_login(_update(text="/login"), ctx)
        assert br.dialogs[1]["acc_id"] == "main"

    async def test_several_accounts_ask_which(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        br.store.put(Account(id="a1", name="Work"))
        br.store.put(Account(id="a2", name="Home"))
        ctx = _ctx(br)
        ctx.args = ["+79991234567"]
        u = _update(text="/login")
        await sb._cmd_login(u, ctx)
        kb = u.effective_message.reply_text.await_args.kwargs["reply_markup"]
        datas = [b.callback_data for r in kb.inline_keyboard for b in r]
        assert datas == ["acc:phone:a1", "acc:phone:a2", "acc:phone:a3"]
        assert 1 not in br.dialogs

    async def test_first_login_claims_owner(self, tmp_path):
        br = _bridge(tmp_path)
        ctx = _ctx(br)
        ctx.args = []
        await sb._cmd_login(_update(user_id=9, text="/login"), ctx)
        assert br.owner_id == 9

    async def test_stranger_cannot_login(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        ctx.args = ["+79991234567"]
        with patch.object(sb, "PhoneLogin") as pl:
            await sb._cmd_login(_update(user_id=2), ctx)
        pl.assert_not_called()

    async def test_deep_link_from_group_starts_login(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        ctx.args = ["login"]
        u = _update(text="/start login")
        await sb._cmd_start(u, ctx)
        assert br.dialogs[1]["step"] == "phone"

    async def test_login_in_group_points_to_private_chat(self, tmp_path):
        br = _bridge(tmp_path, owner=1)
        ctx = _ctx(br)
        ctx.bot.username = "bridge_bot"
        ctx.bot.send_message = AsyncMock()
        u = _update(text="/login +79991234567")
        u.effective_message.chat_id = -100
        u.effective_message.is_topic_message = False
        await sb._cmd_login_in_group(u, ctx)
        u.effective_message.delete.assert_awaited()           # number wiped
        kw = ctx.bot.send_message.await_args.kwargs
        url = kw["reply_markup"].inline_keyboard[0][0].url
        assert url == "https://t.me/bridge_bot?start=login"
        assert 1 not in br.dialogs                             # nothing sent to MAX


async def test_publish_commands_sets_private_and_group_menus():
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()
    await sb.publish_commands(bot)
    scopes = [type(c.kwargs["scope"]).__name__ for c in bot.set_my_commands.await_args_list]
    assert scopes == ["BotCommandScopeAllPrivateChats", "BotCommandScopeAllGroupChats"]
    private = [c.command for c in bot.set_my_commands.await_args_list[0].args[0]]
    assert "login" in private
