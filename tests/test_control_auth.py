from __future__ import annotations

import sqlite3
import time

import pytest

from factory.control.auth import AuthError, AuthStore


PASSWORD = "correct horse battery staple"


def test_create_login_and_authenticate(tmp_path):
    store = AuthStore(tmp_path / "nested" / "users.db")
    user = store.create_user(" Alice ", PASSWORD)
    token, csrf_token, login_user = store.login("ALICE", PASSWORD)

    assert login_user == user == {"id": 1, "username": "alice"}
    assert csrf_token
    assert store.authenticate(token) == {
        "id": 1,
        "username": "alice",
        "csrf_token": csrf_token,
        "expires_at": pytest.approx(time.time() + 12 * 60 * 60, abs=2),
    }


def test_wrong_password_and_unknown_user_are_unauthorized(tmp_path):
    store = AuthStore(tmp_path / "users.db")
    store.create_user("alice", PASSWORD)
    with pytest.raises(AuthError) as wrong:
        store.login("alice", "wrong password, definitely")
    with pytest.raises(AuthError) as unknown:
        store.login("nobody", PASSWORD)
    assert wrong.value.status == unknown.value.status == 401


def test_logout_persists_across_store_instances(tmp_path):
    path = tmp_path / "users.db"
    store = AuthStore(path)
    store.create_user("alice", PASSWORD)
    token, _, _ = store.login("alice", PASSWORD)
    store.logout(token)
    assert AuthStore(path).authenticate(token) is None


def test_expired_session_is_not_authenticated(tmp_path):
    path = tmp_path / "users.db"
    store = AuthStore(path)
    store.create_user("alice", PASSWORD)
    token, _, _ = store.login("alice", PASSWORD)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
    assert store.authenticate(token) is None


def test_expired_failure_window_can_start_a_new_window(tmp_path):
    path = tmp_path / "users.db"
    store = AuthStore(path)
    store.create_user("alice", PASSWORD)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO login_failures(username, window_started_at, failure_count) VALUES (?, ?, ?)",
            ("alice", time.time() - 5 * 60 - 1, 4),
        )

    with pytest.raises(AuthError) as error:
        store.login("alice", "wrong password, definitely")
    assert error.value.status == 401
    with sqlite3.connect(path) as db:
        count, started = db.execute(
            "SELECT failure_count, window_started_at FROM login_failures WHERE username = ?",
            ("alice",),
        ).fetchone()
    assert count == 1
    assert started > time.time() - 5 * 60


def test_five_failures_throttle_username(tmp_path):
    store = AuthStore(tmp_path / "users.db")
    store.create_user("alice", PASSWORD)
    for _ in range(5):
        with pytest.raises(AuthError) as error:
            store.login("alice", "wrong password, definitely")
        assert error.value.status == 401
    with pytest.raises(AuthError) as throttled:
        store.login("alice", PASSWORD)
    assert throttled.value.status == 429


def test_password_hash_is_never_plaintext_or_returned(tmp_path):
    path = tmp_path / "users.db"
    store = AuthStore(path)
    user = store.create_user("alice", PASSWORD)
    token, csrf_token, login_user = store.login("alice", PASSWORD)
    assert "password_hash" not in user
    assert "password_hash" not in login_user
    assert PASSWORD not in path.read_bytes().decode("utf-8", errors="ignore")
    assert PASSWORD not in repr(store.authenticate(token))
    assert csrf_token


def test_username_is_parameterized_and_duplicate_normalized(tmp_path):
    store = AuthStore(tmp_path / "users.db")
    injected = "a' OR 1=1 --"
    user = store.create_user(injected, PASSWORD)
    assert store.login(injected, PASSWORD)[2] == user
    with pytest.raises(AuthError) as duplicate:
        store.create_user("  A' OR 1=1 -- ", PASSWORD)
    assert duplicate.value.status == 409
