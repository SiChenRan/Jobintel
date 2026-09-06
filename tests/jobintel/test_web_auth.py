from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from jobintel.config import JobIntelSettings
from jobintel.persistence.db import JobIntelDatabase
from jobintel.web.app import create_app
from jobintel.web.auth import WebAuthStore

_USERNAME = "test-admin"
_PASSWORD = "test-password-123"


def _settings(path: Path) -> JobIntelSettings:
    return JobIntelSettings(jobintel_db_path=path)


def _bootstrap(client: TestClient) -> str:
    response = client.post(
        "/api/auth/bootstrap",
        json={"username": _USERNAME, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def _login(settings: JobIntelSettings, username: str, password: str) -> TestClient:
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


def _register(
    settings: JobIntelSettings,
    *,
    username: str,
    password: str,
    display_name: str,
    email: str,
) -> tuple[TestClient, dict[str, object]]:
    client = TestClient(create_app(settings))
    response = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": password,
            "display_name": display_name,
            "email": email,
        },
    )
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client, response.json()["user"]


def test_web_requires_login_and_bootstraps_only_one_administrator(tmp_path: Path) -> None:
    database_path = tmp_path / "auth.db"
    client = TestClient(create_app(_settings(database_path)))

    redirect = client.get("/", follow_redirects=False)
    static_shell = client.get("/static/index.html", follow_redirects=False)
    static_script = client.get("/static/app.js", follow_redirects=False)
    unauthorized = client.get("/api/dashboard")
    initial_status = client.get("/api/auth/status").json()
    registration_before_setup = client.post(
        "/api/auth/register",
        json={
            "username": "candidate-user",
            "password": "candidate-password-1",
            "display_name": "候选人",
            "email": "candidate@example.com",
        },
    )

    assert redirect.status_code == 303
    assert redirect.headers["location"] == "/login"
    assert static_shell.status_code == 303
    assert static_script.status_code == 303
    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"
    assert registration_before_setup.status_code == 409
    assert initial_status == {
        "authenticated": False,
        "setup_required": True,
        "user": None,
        "csrf_token": None,
        "expires_at": None,
    }
    login_response = client.get("/login")
    login_page = login_response.text
    login_script = client.get("/static/login.js").text
    login_styles = client.get("/static/app.css").text
    assert 'id="auth-form"' in login_page
    assert 'id="auth-switch"' in login_page
    assert 'id="display-name-field" class="registration-only" hidden' in login_page
    assert 'id="email-field" class="registration-only" hidden' in login_page
    assert 'id="confirm-password-field" class="new-account-only" hidden' in login_page
    assert "/api/auth/register" in login_script
    assert "displayNameField.hidden = !isRegister" in login_script
    assert "emailField.hidden = !isRegister" in login_script
    assert "confirmField.hidden = !needsNewPassword" in login_script
    assert "[hidden] { display: none !important; }" in login_styles
    assert login_response.headers["cache-control"] == "no-store"

    response = client.post(
        "/api/auth/bootstrap",
        json={"username": _USERNAME, "password": _PASSWORD},
    )

    assert response.status_code == 200
    assert response.json()["user"]["username"] == _USERNAME
    assert response.json()["user"]["role"] == "admin"
    assert response.json()["user"]["candidate_id"] is None
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert client.get("/api/admin/users").status_code == 200
    assert client.get("/api/dashboard").status_code == 403
    duplicate = client.post(
        "/api/auth/bootstrap",
        json={"username": "other-admin", "password": "another-password-123"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "AUTH_SETUP_ALREADY_COMPLETED"

    database = JobIntelDatabase.connect(database_path)
    row = database.connection.execute(
        "SELECT password_hash FROM web_users WHERE username = ?", (_USERNAME,)
    ).fetchone()
    database.close()
    assert row is not None
    assert row["password_hash"].startswith("scrypt$")
    assert _PASSWORD not in row["password_hash"]


def test_web_enforces_csrf_logout_and_fresh_login(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path / "csrf.db")))
    csrf_token = _bootstrap(client)

    rejected = client.post("/api/auth/logout")
    assert rejected.status_code == 403
    assert rejected.json()["detail"]["code"] == "CSRF_VALIDATION_FAILED"

    client.headers["X-CSRF-Token"] = csrf_token
    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 200
    assert client.get("/api/dashboard").status_code == 401

    client.headers.pop("X-CSRF-Token")
    invalid = client.post(
        "/api/auth/login",
        json={"username": _USERNAME, "password": "incorrect-password"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["detail"]["code"] == "INVALID_LOGIN"

    authenticated = client.post(
        "/api/auth/login",
        json={"username": "TEST-ADMIN", "password": _PASSWORD},
    )
    assert authenticated.status_code == 200
    assert authenticated.json()["authenticated"] is True
    assert client.get("/api/admin/users").status_code == 200


def test_expired_server_session_cannot_access_api(tmp_path: Path) -> None:
    database_path = tmp_path / "expired.db"
    client = TestClient(create_app(_settings(database_path)))
    _bootstrap(client)

    database = JobIntelDatabase.connect(database_path)
    database.connection.execute(
        "UPDATE web_sessions SET expires_at = ?",
        ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(),),
    )
    database.connection.commit()
    database.close()

    response = client.get("/api/admin/users")
    assert response.status_code == 401
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_local_password_reset_revokes_sessions_and_replaces_password(tmp_path: Path) -> None:
    database_path = tmp_path / "reset.db"
    client = TestClient(create_app(_settings(database_path)))
    _bootstrap(client)

    database = JobIntelDatabase.connect(database_path)
    user = WebAuthStore(database, session_hours=168).reset_password(
        "TEST-ADMIN", "replacement-password-456"
    )
    database.close()

    assert user.username == _USERNAME
    assert client.get("/api/admin/users").status_code == 401
    old_password = client.post(
        "/api/auth/login",
        json={"username": _USERNAME, "password": _PASSWORD},
    )
    assert old_password.status_code == 401
    new_password = client.post(
        "/api/auth/login",
        json={"username": _USERNAME, "password": "replacement-password-456"},
    )
    assert new_password.status_code == 200


def test_login_is_rate_limited_after_repeated_failures(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path / "throttle.db")))
    csrf_token = _bootstrap(client)
    client.headers["X-CSRF-Token"] = csrf_token
    assert client.post("/api/auth/logout").status_code == 200
    client.headers.pop("X-CSRF-Token")

    for _ in range(5):
        response = client.post(
            "/api/auth/login",
            json={"username": _USERNAME, "password": "incorrect-password"},
        )
        assert response.status_code == 401
    blocked = client.post(
        "/api/auth/login",
        json={"username": _USERNAME, "password": _PASSWORD},
    )

    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "LOGIN_RATE_LIMITED"
    assert int(blocked.headers["retry-after"]) >= 1


def test_candidates_self_register_and_admin_manages_registered_users(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "roles.db")
    admin = TestClient(create_app(settings))
    admin.headers["X-CSRF-Token"] = _bootstrap(admin)
    assert admin.get("/api/dashboard").status_code == 403
    assert admin.get("/api/admin/environment").status_code == 200
    for business_path in (
        "/api/profiles",
        "/api/discoveries",
        "/api/analyses",
        "/api/outreach-drafts",
        "/api/radar/checks",
    ):
        assert admin.get(business_path).status_code == 403
    assert [item["role"] for item in admin.get("/api/admin/users").json()] == ["admin"]

    candidate, first = _register(
        settings,
        username="candidate-one",
        password="candidate-password-1",
        display_name="候选人一号",
        email="candidate-one@example.com",
    )
    _second_client, second = _register(
        settings,
        username="candidate-two",
        password="candidate-password-2",
        display_name="候选人二号",
        email="candidate-two@example.com",
    )
    duplicate = TestClient(create_app(settings)).post(
        "/api/auth/register",
        json={
            "username": "candidate-copy",
            "password": "candidate-password-3",
            "display_name": "重复邮箱",
            "email": "CANDIDATE-ONE@example.com",
        },
    )

    assert first["role"] == "candidate"
    assert str(first["candidate_id"]).startswith("candidate_")
    assert first["candidate_id"] != second["candidate_id"]
    assert duplicate.status_code == 409
    users = admin.get("/api/admin/users").json()
    assert {item["username"] for item in users} == {
        "test-admin",
        "candidate-one",
        "candidate-two",
    }
    assert all(item["account_status"] == "active" for item in users)
    assert "password" not in str(users).casefold()

    status = candidate.get("/api/auth/status").json()
    assert status["user"]["role"] == "candidate"
    assert status["user"]["candidate_id"] == first["candidate_id"]
    assert candidate.get("/api/profiles").json() == []
    assert candidate.get(f"/api/profiles/{second['candidate_id']}").status_code == 403
    assert candidate.get("/api/admin/users").status_code == 403
    assert candidate.get("/api/admin/environment").status_code == 403
    denied_search = candidate.post(
        "/api/discoveries",
        json={"candidate_id": second["candidate_id"], "query": "Python", "detail_top": 0},
    )
    assert denied_search.status_code == 403

    updated = admin.patch(
        f"/api/admin/users/{first['user_id']}",
        json={"display_name": "更新后的姓名", "email": "updated@example.com"},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "更新后的姓名"
    assert candidate.get("/api/auth/status").json()["user"]["email"] == "updated@example.com"

    own_update = candidate.patch(
        "/api/auth/profile",
        json={"display_name": "自己修改的姓名", "email": "self-updated@example.com"},
    )
    assert own_update.status_code == 200
    assert own_update.json()["display_name"] == "自己修改的姓名"
    managed = admin.get("/api/admin/users").json()
    assert next(item for item in managed if item["user_id"] == first["user_id"])["email"] == (
        "self-updated@example.com"
    )

    disabled = admin.patch(
        f"/api/admin/users/{first['user_id']}/status",
        json={"is_active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_active"] is False
    assert candidate.get("/api/dashboard").status_code == 401
    rejected_login = candidate.post(
        "/api/auth/login",
        json={"username": "candidate-one", "password": "candidate-password-1"},
    )
    assert rejected_login.status_code == 401


def test_admin_updates_runtime_environment_without_exposing_secrets(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "environment.db")
    admin = TestClient(create_app(settings))
    admin.headers["X-CSRF-Token"] = _bootstrap(admin)

    updated = admin.put(
        "/api/admin/environment",
        json={
            "llm_provider": "deepseek",
            "deepseek_model": "deepseek-chat",
            "deepseek_api_key": "secret-deepseek-key",
            "discovery_cdp_port": 9333,
            "discovery_search_min_delay_seconds": 2.0,
            "discovery_search_max_delay_seconds": 3.0,
        },
    )

    assert updated.status_code == 200, updated.text
    payload = updated.json()
    assert payload["llm_provider"] == "deepseek"
    assert payload["deepseek_model"] == "deepseek-chat"
    assert payload["deepseek_api_key_configured"] is True
    assert payload["discovery_cdp_port"] == 9333
    assert "secret-deepseek-key" not in str(payload)
    loaded = admin.get("/api/admin/environment").json()
    assert loaded["deepseek_api_key_configured"] is True
    assert "secret-deepseek-key" not in str(loaded)
    assert admin.get("/api/health").json()["provider"] == "deepseek"

    invalid = admin.put(
        "/api/admin/environment",
        json={
            "discovery_search_min_delay_seconds": 5.0,
            "discovery_search_max_delay_seconds": 2.0,
        },
    )
    assert invalid.status_code == 422


def test_candidate_changes_password_and_admin_can_reset_it(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "password-lifecycle.db")
    admin = TestClient(create_app(settings))
    admin.headers["X-CSRF-Token"] = _bootstrap(admin)
    candidate, created = _register(
        settings,
        username="candidate-user",
        password="initial-password-1",
        display_name="候选人",
        email="candidate@example.com",
    )
    changed = candidate.post(
        "/api/auth/change-password",
        json={
            "current_password": "initial-password-1",
            "new_password": "candidate-password-2",
        },
    )
    assert changed.status_code == 200
    candidate.headers["X-CSRF-Token"] = changed.json()["csrf_token"]
    assert candidate.get("/api/dashboard").status_code == 200

    reset = admin.post(
        f"/api/admin/users/{created['user_id']}/reset-password",
        json={"password": "admin-reset-password-3"},
    )
    assert reset.status_code == 200
    assert candidate.get("/api/dashboard").status_code == 401

    old_login = candidate.post(
        "/api/auth/login",
        json={"username": "candidate-user", "password": "candidate-password-2"},
    )
    assert old_login.status_code == 401
    new_login = candidate.post(
        "/api/auth/login",
        json={"username": "candidate-user", "password": "admin-reset-password-3"},
    )
    assert new_login.status_code == 200
