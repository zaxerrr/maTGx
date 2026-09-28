"""Tests for app/accounts.py — account/owner/group registry."""

import os

from app.accounts import LEGACY_ID, Account, AccountStore, fingerprint


def _store(tmp_path):
    return AccountStore(str(tmp_path / "accounts.json"))


def test_persistence_and_permissions(tmp_path):
    s = _store(tmp_path)
    s.set_owner(42)
    s.put(Account(id="a1", name="Ann", token="t", device_id="d", group_id=-100))
    s.remember_group(-100, title="G", is_forum=True, can_manage_topics=True)
    assert oct(os.stat(tmp_path / "accounts.json").st_mode & 0o777) == "0o600"

    s2 = _store(tmp_path)
    assert s2.owner_id == 42
    acc = s2.get("a1")
    assert acc.title == "Ann" and acc.has_credentials and acc.group_id == -100
    assert s2.get_group(-100).ready


def test_new_id_and_groups(tmp_path):
    s = _store(tmp_path)
    assert s.new_id() == "a1"
    s.put(Account(id="a1"))
    assert s.new_id() == "a2"
    s.put(Account(id="a2", group_id=5))
    s.put(Account(id="a3", group_id=5))
    assert {a.id for a in s.accounts_in_group(5)} == {"a2", "a3"}
    s.remove("a2")
    assert s.get("a2") is None


def test_group_readiness(tmp_path):
    s = _store(tmp_path)
    g = s.remember_group(1, title="x")
    assert not g.ready
    g = s.remember_group(1, is_forum=True, can_manage_topics=False)
    assert not g.ready and g.title == "x"
    s.forget_group(1)
    assert s.get_group(1) is None


class TestBootstrapFromEnv:
    def test_creates_main_account(self, tmp_path):
        s = _store(tmp_path)
        acc = s.bootstrap_from_env("tok", "dev", -100, "-1,-2")
        assert acc.id == LEGACY_ID and acc.token == "tok" and acc.group_id == -100
        assert acc.max_chat_ids == "-1,-2"

    def test_nothing_in_env(self, tmp_path):
        assert _store(tmp_path).bootstrap_from_env(None, None, None, None) is None

    def test_same_env_token_does_not_override_chat_login(self, tmp_path):
        s = _store(tmp_path)
        s.bootstrap_from_env("old", "dev", -100, None)
        acc = s.get(LEGACY_ID)
        acc.token, acc.device_id = "fresh-from-chat", "dev2"
        s.put(acc)
        s.bootstrap_from_env("old", "dev", -100, None)        # restart, same .env
        assert s.get(LEGACY_ID).token == "fresh-from-chat"

    def test_changed_env_token_wins(self, tmp_path):
        s = _store(tmp_path)
        s.bootstrap_from_env("old", "dev", -100, None)
        s.bootstrap_from_env("new", "dev", -100, None)
        acc = s.get(LEGACY_ID)
        assert acc.token == "new" and acc.env_fingerprint == fingerprint("new")

    def test_group_from_env_does_not_override_chosen_group(self, tmp_path):
        s = _store(tmp_path)
        s.bootstrap_from_env("t", "d", -100, None)
        acc = s.get(LEGACY_ID)
        acc.group_id = -200
        s.put(acc)
        s.bootstrap_from_env("t", "d", -100, None)
        assert s.get(LEGACY_ID).group_id == -200
