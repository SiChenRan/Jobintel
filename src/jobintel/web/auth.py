"""Password authentication and server-side sessions for the JobIntel Web UI."""

from __future__ import annotations

import base64
import hashlib
import secrets
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from jobintel.notifications.address import validate_email_address
from jobintel.persistence.db import JobIntelDatabase

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LENGTH = 32
_DUMMY_PASSWORD_HASH = "$".join(
    (
        "scrypt",
        str(_SCRYPT_N),
        str(_SCRYPT_R),
        str(_SCRYPT_P),
        base64.urlsafe_b64encode(bytes(16)).decode("ascii"),
        base64.urlsafe_b64encode(
            hashlib.scrypt(
                b"jobintel-invalid-password",
                salt=bytes(16),
                n=_SCRYPT_N,
                r=_SCRYPT_R,
                p=_SCRYPT_P,
                dklen=_SCRYPT_LENGTH,
            )
        ).decode("ascii"),
    )
)


class AuthenticationError(ValueError):
    """Raised when a username and password pair cannot be authenticated."""


class BootstrapClosedError(ValueError):
    """Raised when someone tries to create a second initial administrator."""


class LoginRateLimitError(ValueError):
    """Raised when one client exceeds the bounded failed-login window."""

    def __init__(self, retry_after_seconds: int) -> None:
        """Carry the minimum retry delay for an HTTP Retry-After header."""
        super().__init__(f"登录失败次数过多, 请在 {retry_after_seconds} 秒后重试")
        self.retry_after_seconds = retry_after_seconds


class WebRole(StrEnum):
    """Authorization roles available in the browser workspace."""

    ADMIN = "admin"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class WebUser:
    """Minimal authenticated identity exposed to Web request handlers."""

    user_id: str
    username: str
    display_name: str
    email: str | None
    role: WebRole
    candidate_id: str | None
    is_active: bool


@dataclass(frozen=True)
class ManagedWebUser:
    """Administrative user projection including non-secret account metadata."""

    user: WebUser
    created_at: datetime
    last_login_at: datetime | None


@dataclass(frozen=True)
class WebSession:
    """Resolved server-side session with the CSRF value needed by the browser."""

    user: WebUser
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class CreatedWebSession:
    """A newly issued raw cookie and its persisted session projection."""

    cookie_token: str
    session: WebSession


class LoginThrottle:
    """Small per-process throttle that slows online password guessing."""

    def __init__(self, *, max_failures: int = 5, window_seconds: int = 300) -> None:
        """Configure a bounded failure count and rolling time window."""
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, client_key: str) -> None:
        """Raise with a retry delay if a client has too many recent failures."""
        now = time.monotonic()
        with self._lock:
            recent = self._recent(client_key, now)
            if len(recent) >= self._max_failures:
                retry_after = max(1, round(self._window_seconds - (now - recent[0])))
                raise LoginRateLimitError(retry_after)

    def failed(self, client_key: str) -> None:
        """Record one failed login for a client."""
        now = time.monotonic()
        with self._lock:
            recent = self._recent(client_key, now)
            recent.append(now)
            self._failures[client_key] = recent

    def succeeded(self, client_key: str) -> None:
        """Clear failures after valid authentication."""
        with self._lock:
            self._failures.pop(client_key, None)

    def _recent(self, client_key: str, now: float) -> list[float]:
        cutoff = now - self._window_seconds
        recent = [value for value in self._failures.get(client_key, ()) if value > cutoff]
        if recent:
            self._failures[client_key] = recent
        else:
            self._failures.pop(client_key, None)
        return recent


def normalize_username(username: str) -> tuple[str, str]:
    """Return a display username and a stable comparison key."""
    display = unicodedata.normalize("NFKC", username).strip()
    if not 3 <= len(display) <= 50:
        raise ValueError("用户名长度必须为 3 到 50 个字符")
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in display
    ):
        raise ValueError("用户名不能包含空格或控制字符")
    return display, display.casefold()


def validate_password(password: str) -> str:
    """Enforce a bounded password before running the expensive password hash."""
    if not 10 <= len(password) <= 128:
        raise ValueError("密码长度必须为 10 到 128 个字符")
    return password


def validate_display_name(display_name: str) -> str:
    """Normalize a human-readable account name while allowing ordinary spaces."""
    value = unicodedata.normalize("NFKC", display_name).strip()
    if not 1 <= len(value) <= 80:
        raise ValueError("姓名长度必须为 1 到 80 个字符")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise ValueError("姓名不能包含控制字符")
    return value


def normalize_account_email(email: str) -> tuple[str, str]:
    """Return a validated display email and a case-insensitive uniqueness key."""
    try:
        value = validate_email_address(email, label="account")
    except ValueError as exc:
        raise ValueError("请输入有效的邮箱地址") from exc
    return value, value.casefold()


def hash_password(password: str) -> str:
    """Hash a validated password with a unique salt and memory-hard scrypt."""
    validate_password(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LENGTH,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify an encoded scrypt password without raising on malformed storage."""
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$")
        if scheme != "scrypt":
            return False
        if (int(raw_n), int(raw_r), int(raw_p)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(raw_salt.encode("ascii")),
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual, expected)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class WebAuthStore:
    """Persist users and opaque Web sessions in the main SQLite database."""

    def __init__(self, database: JobIntelDatabase, *, session_hours: int) -> None:
        """Bind a migrated database and a bounded session lifetime."""
        self._database = database
        self._session_hours = session_hours

    @property
    def _conn(self) -> sqlite3.Connection:
        return self._database.connection

    def has_users(self) -> bool:
        """Return whether the one-time administrator setup has completed."""
        row = self._conn.execute("SELECT EXISTS(SELECT 1 FROM web_users)").fetchone()
        return bool(row[0])

    def bootstrap(self, username: str, password: str) -> CreatedWebSession:
        """Atomically create the first administrator and its initial session."""
        display, normalized = normalize_username(username)
        encoded = hash_password(password)
        now = datetime.now(UTC)
        user = WebUser(
            user_id=f"user_{uuid.uuid4().hex}",
            username=display,
            display_name=display,
            email=None,
            role=WebRole.ADMIN,
            candidate_id=None,
            is_active=True,
        )
        created = self._new_session(user, now=now)
        with self._database.transaction():
            if self.has_users():
                raise BootstrapClosedError("管理员账户已经创建, 请直接登录")
            self._conn.execute(
                """
                INSERT INTO web_users (
                    user_id, username, username_normalized, password_hash, created_at,
                    display_name
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user.user_id, display, normalized, encoded, now.isoformat(), display),
            )
            self._insert_session(created, now=now)
        return created

    def login(self, username: str, password: str) -> CreatedWebSession:
        """Authenticate credentials and rotate into a fresh server-side session."""
        _, normalized = normalize_username(username)
        validate_password(password)
        row = self._conn.execute(
            """
            SELECT user_id, username, display_name, email, password_hash,
                   role, candidate_id, is_active
            FROM web_users WHERE username_normalized = ? AND is_active = 1
            """,
            (normalized,),
        ).fetchone()
        encoded = _DUMMY_PASSWORD_HASH if row is None else str(row["password_hash"])
        password_matches = verify_password(password, encoded)
        if row is None or not password_matches:
            raise AuthenticationError("用户名或密码错误")
        now = datetime.now(UTC)
        user = self._user_from_row(row)
        created = self._new_session(user, now=now)
        with self._database.transaction():
            self._conn.execute(
                "UPDATE web_users SET last_login_at = ? WHERE user_id = ?",
                (now.isoformat(), user.user_id),
            )
            self._delete_expired(now)
            self._insert_session(created, now=now)
        return created

    def resolve(self, cookie_token: str | None) -> WebSession | None:
        """Resolve one non-expired opaque cookie into its authenticated identity."""
        if not cookie_token:
            return None
        now = datetime.now(UTC)
        row = self._conn.execute(
            """
            SELECT u.user_id, u.username, u.display_name, u.email,
                   u.role, u.candidate_id, u.is_active,
                   s.csrf_token, s.expires_at
            FROM web_sessions s
            JOIN web_users u ON u.user_id = s.user_id
            WHERE s.session_digest = ? AND s.expires_at > ? AND u.is_active = 1
            """,
            (_token_digest(cookie_token), now.isoformat()),
        ).fetchone()
        if row is None:
            return None
        return WebSession(
            user=self._user_from_row(row),
            csrf_token=str(row["csrf_token"]),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
        )

    def logout(self, cookie_token: str | None) -> None:
        """Revoke one session without affecting the user's other browsers."""
        if not cookie_token:
            return
        with self._database.transaction():
            self._conn.execute(
                "DELETE FROM web_sessions WHERE session_digest = ?",
                (_token_digest(cookie_token),),
            )

    def reset_password(self, username: str, password: str) -> WebUser:
        """Replace a local user's password and revoke every active session."""
        _, normalized = normalize_username(username)
        encoded = hash_password(password)
        with self._database.transaction():
            row = self._conn.execute(
                """
                SELECT user_id, username, display_name, email, role, candidate_id, is_active
                FROM web_users WHERE username_normalized = ?
                """,
                (normalized,),
            ).fetchone()
            if row is None:
                raise AuthenticationError("用户不存在")
            user = self._user_from_row(row)
            self._conn.execute(
                "UPDATE web_users SET password_hash = ? WHERE user_id = ?",
                (encoded, user.user_id),
            )
            self._conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user.user_id,))
        return user

    def register_candidate(
        self,
        username: str,
        password: str,
        display_name: str,
        email: str,
    ) -> CreatedWebSession:
        """Register a candidate and issue an isolated server-owned identity."""
        display, normalized = normalize_username(username)
        name = validate_display_name(display_name)
        email_value, email_normalized = normalize_account_email(email)
        encoded = hash_password(password)
        now = datetime.now(UTC)
        bound_candidate = f"candidate_{uuid.uuid4().hex}"
        user = WebUser(
            user_id=f"user_{uuid.uuid4().hex}",
            username=display,
            display_name=name,
            email=email_value,
            role=WebRole.CANDIDATE,
            candidate_id=bound_candidate,
            is_active=True,
        )
        created = self._new_session(user, now=now)
        try:
            with self._database.transaction():
                self._conn.execute(
                    """
                    INSERT INTO web_users (
                        user_id, username, username_normalized, password_hash,
                        created_at, role, candidate_id, is_active,
                        display_name, email, email_normalized
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        user.user_id,
                        display,
                        normalized,
                        encoded,
                        now.isoformat(),
                        user.role.value,
                        bound_candidate,
                        name,
                        email_value,
                        email_normalized,
                    ),
                )
                self._insert_session(created, now=now)
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名或邮箱已被注册") from exc
        return created

    def list_users(self) -> tuple[ManagedWebUser, ...]:
        """List accounts without password hashes or session secrets."""
        rows = self._conn.execute(
            """
            SELECT user_id, username, display_name, email, role, candidate_id,
                   is_active, created_at, last_login_at
            FROM web_users ORDER BY role, created_at, user_id
            """
        ).fetchall()
        return tuple(
            ManagedWebUser(
                user=self._user_from_row(row),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                last_login_at=(
                    datetime.fromisoformat(str(row["last_login_at"]))
                    if row["last_login_at"] is not None
                    else None
                ),
            )
            for row in rows
        )

    def get_user(self, user_id: str) -> WebUser:
        """Return one account by its server-issued identifier."""
        row = self._conn.execute(
            """
            SELECT user_id, username, display_name, email, role, candidate_id, is_active
            FROM web_users WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            raise AuthenticationError("用户不存在")
        return self._user_from_row(row)

    def update_user_profile(self, user_id: str, display_name: str, email: str) -> WebUser:
        """Update editable account information without changing identity or role."""
        name = validate_display_name(display_name)
        email_value, email_normalized = normalize_account_email(email)
        try:
            with self._database.transaction():
                user = self.get_user(user_id)
                self._conn.execute(
                    """
                    UPDATE web_users
                    SET display_name = ?, email = ?, email_normalized = ?
                    WHERE user_id = ?
                    """,
                    (name, email_value, email_normalized, user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该邮箱已被其他用户使用") from exc
        return WebUser(
            user_id=user.user_id,
            username=user.username,
            display_name=name,
            email=email_value,
            role=user.role,
            candidate_id=user.candidate_id,
            is_active=user.is_active,
        )

    def set_candidate_user_active(self, user_id: str, is_active: bool) -> WebUser:
        """Enable or disable a candidate account and revoke it when disabled."""
        with self._database.transaction():
            user = self.get_user(user_id)
            if user.role is not WebRole.CANDIDATE:
                raise ValueError("不能通过候选人管理接口修改管理员")
            self._conn.execute(
                "UPDATE web_users SET is_active = ? WHERE user_id = ?",
                (int(is_active), user_id),
            )
            if not is_active:
                self._conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
        return WebUser(
            user_id=user.user_id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            role=user.role,
            candidate_id=user.candidate_id,
            is_active=is_active,
        )

    def reset_candidate_password(self, user_id: str, password: str) -> WebUser:
        """Let an administrator reset a candidate password and revoke sessions."""
        user = self.get_user(user_id)
        if user.role is not WebRole.CANDIDATE:
            raise ValueError("不能通过候选人管理接口重置管理员密码")
        return self.reset_password(user.username, password)

    def change_password(
        self, user_id: str, current_password: str, new_password: str
    ) -> CreatedWebSession:
        """Verify and change one's own password while rotating every session."""
        validate_password(current_password)
        encoded = hash_password(new_password)
        row = self._conn.execute(
            """
            SELECT user_id, username, display_name, email, role,
                   candidate_id, is_active, password_hash
            FROM web_users WHERE user_id = ? AND is_active = 1
            """,
            (user_id,),
        ).fetchone()
        if row is None or not verify_password(current_password, str(row["password_hash"])):
            raise AuthenticationError("当前密码错误")
        user = self._user_from_row(row)
        now = datetime.now(UTC)
        created = self._new_session(user, now=now)
        with self._database.transaction():
            self._conn.execute(
                "UPDATE web_users SET password_hash = ? WHERE user_id = ?",
                (encoded, user_id),
            )
            self._conn.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
            self._insert_session(created, now=now)
        return created

    def _new_session(self, user: WebUser, *, now: datetime) -> CreatedWebSession:
        cookie_token = secrets.token_urlsafe(32)
        return CreatedWebSession(
            cookie_token=cookie_token,
            session=WebSession(
                user=user,
                csrf_token=secrets.token_urlsafe(32),
                expires_at=now + timedelta(hours=self._session_hours),
            ),
        )

    def _insert_session(self, created: CreatedWebSession, *, now: datetime) -> None:
        self._conn.execute(
            """
            INSERT INTO web_sessions (
                session_digest, user_id, csrf_token, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                _token_digest(created.cookie_token),
                created.session.user.user_id,
                created.session.csrf_token,
                now.isoformat(),
                created.session.expires_at.isoformat(),
            ),
        )

    def _delete_expired(self, now: datetime) -> None:
        self._conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now.isoformat(),))

    @staticmethod
    def _user_from_row(row: sqlite3.Row) -> WebUser:
        return WebUser(
            user_id=str(row["user_id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            email=(str(row["email"]) if row["email"] is not None else None),
            role=WebRole(str(row["role"])),
            candidate_id=(str(row["candidate_id"]) if row["candidate_id"] is not None else None),
            is_active=bool(row["is_active"]),
        )
