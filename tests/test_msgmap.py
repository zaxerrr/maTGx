"""Tests for app/msgmap.py — Telegram ↔ Max message id map."""

from app import msgmap as mm_mod
from app.msgmap import MessageMap


def _mm(tmp_path):
    return MessageMap(str(tmp_path / "messages.db"))


def test_roundtrip_both_directions(tmp_path):
    m = _mm(tmp_path)
    m.add(-100, "555", 7, "hello")
    assert m.tg_for_max(-100, "555") == 7
    assert m.tg_for_max(-100, 555) == 7          # int/str ids are equivalent
    assert m.max_for_tg(7) == (-100, "555")       # chat id restored as int
    assert m.text_for_max(-100, "555") == "hello"


def test_unknown_ids(tmp_path):
    m = _mm(tmp_path)
    assert m.tg_for_max(1, "x") is None
    assert m.max_for_tg(99) is None


def test_first_tg_message_wins_for_multipart(tmp_path):
    m = _mm(tmp_path)
    m.add(1, "a", 10, "t")
    m.add(1, "a", 11, "t")
    assert m.tg_for_max(1, "a") == 10


def test_set_text(tmp_path):
    m = _mm(tmp_path)
    m.add(1, "a", 10, "old")
    m.set_text(1, "a", "new")
    assert m.text_for_max(1, "a") == "new"


def test_ignores_empty_ids(tmp_path):
    m = _mm(tmp_path)
    m.add(1, "", 10)
    m.add(1, None, 10)
    assert m.max_for_tg(10) is None


def test_persists_across_instances(tmp_path):
    m = _mm(tmp_path)
    m.add(1, "a", 10)
    m.close()
    assert _mm(tmp_path).tg_for_max(1, "a") == 10


def test_prune_keeps_recent_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(mm_mod, "MAX_ROWS", 5)
    monkeypatch.setattr(mm_mod, "_PRUNE_EVERY", 10)
    m = _mm(tmp_path)
    for i in range(10):
        m.add(1, str(i), i)
    assert m.tg_for_max(1, "0") is None
    assert m.tg_for_max(1, "9") == 9
