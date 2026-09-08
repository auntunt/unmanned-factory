"""Persistent authentication primitives for the factory control plane.

The control API intentionally keeps authentication state separate from the audit
store.  This module uses one short-lived SQLite connection per operation so that
multiple API workers can safely share the same users database.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


# PBKDF2 is available in every supported Python build.  The encoded format keeps
# the parameters with the hash so a future iteration increase can be introduced
# without making existing accounts unusable.
_PBKDF2_ALGORITHM = "sha256"
_PBKDF2_ITERATIONS = 310_000
_SALT_BYTES = 16
_SESSION_BYTES = 32  # 256 bits
_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
_FAILURE_WINDOW_SECONDS = 5 * 60
_MAX_FAILURES = 5
_MAX_PASSWORD_CHARS = 4096


class AuthError(Exception):
    """An expected authentication or account-management error.

    ``status`` is suitable for mapping the error to an HTTP response in the
    control API.  Error messages never include passwords or password-derived
    material.
    """

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


def _normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise AuthError("username must be a string", 400)
    # Canonicalizing both surrounding whitespace and case makes rate limiting
    # and duplicate detection apply to the same account key.
    normalized = username.strip().casefold()
    if not 3 <= len(normalized) <= 80:
        raise AuthError("username must be between 3 and 80 characters", 400)
    return normalized


def _check_password(password: str) -> str:
    if not isinstance(password, str):
        raise AuthError("password must be a string", 400)
    if len(password) < 12:
        raise AuthError("password must be at least 12 characters", 400)
    if len(password) > _MAX_PASSWORD_CHARS:
        raise AuthError("password is too long", 400)
    return password


def _hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    encode = base64.urlsafe_b64encode
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        encode(salt).decode("ascii"),
        encode(digest).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    """Verify an encoded hash, returning false for malformed database data."""
    try:
        scheme, iteration_text, salt_text, digest_text = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iteration_text)
        if iterations <= 0:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            _PBKDF2_ALGORITHM,
            password.encode("utf-8"),
            salt,
            iterations,
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


# A valid, fixed-shape dummy hash makes a lookup for a non-existent account take
# the same password-verification path as a real account.  It is never written to
# the database and contains no user password.
_DUMMY_HASH = _hash_password("factory dummy password")


class AuthStore:
    """SQLite-backed users, login throttling, and revocable sessions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            # Preserve sqlite's transaction context semantics while ensuring
            # the short-lived connection is actually closed after the op.
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_failures (
                    username TEXT PRIMARY KEY,
                    window_started_at REAL NOT NULL,
                    failure_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf_token TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    revoked_at REAL
                );
                CREATE INDEX IF NOT EXISTS sessions_user_id_idx
                    ON sessions(user_id);
                CREATE INDEX IF NOT EXISTS sessions_expires_idx
                    ON sessions(expires_at);
                """
            )
            columns = {row[1] for row in connection.execute('PRAGMA table_info(users)')}
            # Existing installations had trusted owners only.
            if 'role' not in columns:
                connection.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
            if 'active' not in columns:
                connection.execute('ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1')

    @staticmethod
    def _user_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        return {"id": int(row[0]), "username": str(row[1]), "role": str(row[3]), "active": bool(row[4])}

    def create_user(self, username: str, password: str, *, role: str = 'admin') -> dict[str, Any]:
        if role not in ('admin', 'member'):
            raise AuthError('角色无效', 422)
        normalized = _normalize_username(username)
        checked_password = _check_password(password)
        password_hash = _hash_password(checked_password)
        created_at = time.time()
        try:
            with self._connection() as connection:
                cursor = connection.execute(
                    "INSERT INTO users(username, password_hash, created_at, role) VALUES (?, ?, ?, ?)",
                    (normalized, password_hash, created_at, role),
                )
                user_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            raise AuthError("username is already registered", 409) from None
        return {"id": user_id, "username": normalized, "role": role, "active": True}

    def users(self):
        with self._connection() as db:
            return [self._user_dict(row) for row in db.execute(
                'SELECT id,username,password_hash,role,active FROM users ORDER BY id')]

    def update_user(self, user_id: int, *, role: str, active: bool, password: str | None = None):
        if role not in ('admin', 'member') or type(active) is not bool:
            raise AuthError('成员设置无效', 422)
        encoded = _hash_password(_check_password(password)) if password is not None else None
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id,username,password_hash,role,active FROM users WHERE id=?', (user_id,)).fetchone()
            if row is None:
                raise AuthError('成员不存在', 404)
            if row[3] == 'admin' and row[4] and (role != 'admin' or not active):
                if db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    raise AuthError('至少保留一名启用的管理员', 409)
            db.execute('UPDATE users SET role=?,active=?,password_hash=? WHERE id=?',
                       (role, int(active), encoded or row[2], user_id))
            if not active or encoded or role != row[3]:
                db.execute('UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (time.time(), user_id))
            return {'id': user_id, 'username': row[1], 'role': role, 'active': active}

    def change_password(self, user_id: int, current: str, password: str):
        encoded = _hash_password(_check_password(password))
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT password_hash FROM users WHERE id=? AND active=1', (user_id,)).fetchone()
            if row is None or not _verify_password(current, row[0]):
                raise AuthError('当前密码不正确', 403)
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (encoded, user_id))
            db.execute('UPDATE sessions SET revoked_at=? WHERE user_id=?', (time.time(), user_id))

    def login(
        self, username: str, password: str
    ) -> tuple[str, str, dict[str, Any]]:
        normalized = _normalize_username(username)
        if not isinstance(password, str):
            # Keep malformed API input separate from a bad credential while
            # ensuring no value is interpolated into an error message.
            raise AuthError("password must be a string", 400)
        if len(password) > _MAX_PASSWORD_CHARS:
            raise AuthError("password is too long", 400)

        now = time.time()
        # BEGIN IMMEDIATE serializes attempts for the same database.  The
        # password check is deliberately inside this transaction: otherwise
        # concurrent requests could all observe four failures and pass the
        # five-failure limit before any of them committed their update.
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, username, password_hash, role, active FROM users WHERE username = ?",
                (normalized,),
            ).fetchone()
            failure = connection.execute(
                "SELECT window_started_at, failure_count FROM login_failures WHERE username = ?",
                (normalized,),
            ).fetchone()

            if failure is not None:
                window_started, failure_count = float(failure[0]), int(failure[1])
                if now - window_started >= _FAILURE_WINDOW_SECONDS:
                    # Remove the expired row before a failed attempt inserts
                    # the fresh window (username is the primary key).
                    connection.execute(
                        "DELETE FROM login_failures WHERE username = ?",
                        (normalized,),
                    )
                    failure = None
                elif failure_count >= _MAX_FAILURES:
                    connection.rollback()
                    raise AuthError("too many login attempts", 429)

            stored_hash = str(row[2]) if row is not None else _DUMMY_HASH
            valid = _verify_password(password, stored_hash) and row is not None and bool(row[4])
            if not valid:
                if failure is None:
                    connection.execute(
                        "INSERT INTO login_failures(username, window_started_at, failure_count) VALUES (?, ?, 1)",
                        (normalized, now),
                    )
                else:
                    connection.execute(
                        "UPDATE login_failures SET failure_count = ?, window_started_at = ? WHERE username = ?",
                        (int(failure[1]) + 1, now if now - float(failure[0]) >= _FAILURE_WINDOW_SECONDS else failure[0], normalized),
                    )
                connection.commit()
                raise AuthError("invalid credentials", 401)

            # A successful login clears the durable failure window.
            connection.execute("DELETE FROM login_failures WHERE username = ?", (normalized,))
            # ``valid`` can only be true for a real user: the dummy hash is not a
            # hash of any user-supplied password.
            if row is None:  # defensive guard if the dummy value is ever changed
                connection.rollback()
                raise AuthError("invalid credentials", 401)

            token = secrets.token_urlsafe(_SESSION_BYTES)
            csrf_token = secrets.token_urlsafe(_SESSION_BYTES)
            expires_at = now + _SESSION_LIFETIME_SECONDS
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            connection.execute(
                "INSERT INTO sessions(token_hash, user_id, csrf_token, expires_at, revoked_at) VALUES (?, ?, ?, ?, NULL)",
                (token_hash, int(row[0]), csrf_token, expires_at),
            )
            connection.commit()
            return token, csrf_token, self._user_dict(row)

    def authenticate(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.username, sessions.csrf_token, sessions.expires_at, users.role
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token_hash = ?
                  AND sessions.revoked_at IS NULL
                  AND sessions.expires_at > ?
                  AND users.active = 1
                """,
                (token_hash, now),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "username": str(row[1]),
            "csrf_token": str(row[2]),
            "expires_at": float(row[3]),
            "role": str(row[4]),
            "active": True,
        }

    def logout(self, token: str) -> None:
        if not isinstance(token, str) or not token:
            return
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (time.time(), token_hash),
            )
