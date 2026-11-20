import sqlite3

from app import config, db


def test_load_messages_orders_oldest_first_and_respects_limit(tmp_db):
    for i in range(5):
        db.save_message("c1", "user" if i % 2 == 0 else "assistant", f"m{i}")
    # Last 3 of m0..m4 are m2 (user), m3, m4.
    assert db.load_messages("c1", 3) == [
        {"role": "user", "content": "m2"},
        {"role": "assistant", "content": "m3"},
        {"role": "user", "content": "m4"},
    ]


def test_load_messages_drops_leading_assistant(tmp_db):
    db.save_message("c1", "assistant", "a0")
    db.save_message("c1", "user", "u1")
    db.save_message("c1", "assistant", "a2")
    assert db.load_messages("c1", 10) == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a2"},
    ]


def test_load_messages_empty_channel_returns_empty_list(tmp_db):
    assert db.load_messages("nobody", 24) == []


def test_channels_are_isolated(tmp_db):
    db.save_message("c1", "user", "for c1")
    db.save_message("c2", "user", "for c2")
    assert db.load_messages("c1", 10) == [{"role": "user", "content": "for c1"}]
    assert db.load_messages("c2", 10) == [{"role": "user", "content": "for c2"}]


def test_kv_upsert(tmp_db):
    assert db.kv_get("k") is None
    db.kv_set("k", "1")
    db.kv_set("k", "2")
    assert db.kv_get("k") == "2"


def test_init_is_idempotent(tmp_db):
    db.save_message("c1", "user", "kept")
    db.init()
    db.init()
    assert db.load_messages("c1", 10) == [{"role": "user", "content": "kept"}]
    with sqlite3.connect(config.DB_PATH) as conn:
        tables = {
            r[0]
            for r in conn.execute("select name from sqlite_master where type='table'")
        }
    assert {
        "planned_sessions",
        "session_logs",
        "benchmarks",
        "messages",
        "kv",
        "exercises",
    } <= tables
