"""Tests for app/bridge.py — per-account runtimes and routing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.accounts import LEGACY_ID, Account, AccountStore
from app.bridge import Bridge


def _bridge(tmp_path, *accounts, owner=None):
    store = AccountStore(str(tmp_path / "accounts.json"))
    for a in accounts:
        store.put(a)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return Bridge(store, bot, str(tmp_path), env_owner_id=owner)


async def _never():
    await asyncio.Event().wait()


def _patched_run():
    return patch("app.max_client.MaxClient.run", new=lambda self: _never())


class TestLifecycle:
    async def test_starts_only_complete_accounts(self, tmp_path):
        br = _bridge(tmp_path,
                     Account(id="a1", token="t", device_id="d", group_id=-1),
                     Account(id="a2", token="t", device_id="d"),          # no group
                     Account(id="a3", group_id=-1))                       # no token
        with _patched_run():
            await br.start_all()
            assert set(br.runtimes) == {"a1"}
            await br.stop_all()
        assert br.runtimes == {}

    async def test_legacy_account_uses_old_state_files(self, tmp_path):
        br = _bridge(tmp_path, Account(id=LEGACY_ID, token="t", device_id="d", group_id=-1),
                     Account(id="a2", token="t", device_id="d", group_id=-2))
        with _patched_run():
            await br.start_all()
            await br.stop_all()
        assert (tmp_path / "messages.db").exists()
        assert (tmp_path / "accounts" / "a2" / "messages.db").exists()

    async def test_shared_group_gets_title_prefix(self, tmp_path):
        br = _bridge(tmp_path,
                     Account(id="a1", name="Work", token="t", device_id="d", group_id=-1),
                     Account(id="a2", name="Home", token="t", device_id="d", group_id=-1),
                     Account(id="a3", name="Solo", token="t", device_id="d", group_id=-3))
        with _patched_run():
            await br.start_all()
            assert br.runtimes["a1"].sender.title_prefix == "Work · "
            assert br.runtimes["a3"].sender.title_prefix == ""
            await br.stop_all()

    async def test_restart_replaces_runtime(self, tmp_path):
        br = _bridge(tmp_path, Account(id="a1", token="t", device_id="d", group_id=-1))
        with _patched_run():
            first = await br.start_account("a1")
            second = await br.start_account("a1")
            assert first is not second and first.task.cancelled()
            await br.stop_all()


class TestRouting:
    async def test_resolve_topic_by_group_and_thread(self, tmp_path):
        br = _bridge(tmp_path,
                     Account(id="a1", token="t", device_id="d", group_id=-1),
                     Account(id="a2", token="t", device_id="d", group_id=-1))
        with _patched_run():
            await br.start_all()
            br.runtimes["a1"].topic_store.set_topic(111, 5, "x")
            br.runtimes["a2"].topic_store.set_topic(222, 6, "y")
            assert br.resolve_topic(-1, 5).account.id == "a1"
            assert br.resolve_topic(-1, 6).account.id == "a2"
            assert br.resolve_topic(-1, 7) is None
            assert br.resolve_topic(-9, 5) is None
            await br.stop_all()


class TestOwner:
    async def test_env_owner_wins_and_notify(self, tmp_path):
        br = _bridge(tmp_path, owner=7)
        br.store.set_owner(9)
        assert br.owner_id == 7
        await br.notify_owner("hi")
        assert br.bot.send_message.await_args.kwargs["chat_id"] == 7

    async def test_no_owner_no_message(self, tmp_path):
        br = _bridge(tmp_path)
        await br.notify_owner("hi")
        br.bot.send_message.assert_not_awaited()

    async def test_auth_failure_alerts_owner_with_relogin_button(self, tmp_path):
        br = _bridge(tmp_path, Account(id="a1", name="Ann", token="t", device_id="d",
                                       group_id=-1), owner=7)
        with _patched_run():
            rt = await br.start_account("a1")
            rt.sender.send = AsyncMock()
            await rt.client._on_auth_failed_cb("bad token")
            assert rt.status == "auth_failed"
            kw = br.bot.send_message.await_args.kwargs
            assert "Ann" in kw["text"]
            button = kw["reply_markup"].inline_keyboard[0][0]
            assert button.callback_data == "acc:relogin:a1"
            await br.stop_all()


class TestProfileName:
    async def test_generic_name_replaced_from_profile(self, tmp_path):
        br = _bridge(tmp_path, Account(id=LEGACY_ID, name="MAX", token="t", device_id="d",
                                       group_id=-1),
                     Account(id="a2", name="Custom", token="t", device_id="d", group_id=-2))
        with _patched_run():
            await br.start_all()
            snap = {"profile": {"id": 1, "names": [{"firstName": "Ann", "lastName": "K"}]},
                    "chats": []}
            for rt in br.runtimes.values():
                rt.sender.send = AsyncMock()
                await rt.client._on_ready_cb(snap)
            assert br.store.get(LEGACY_ID).name == "Ann K"
            assert br.store.get("a2").name == "Custom"
            assert br.runtimes[LEGACY_ID].status == "online"
            await br.stop_all()
