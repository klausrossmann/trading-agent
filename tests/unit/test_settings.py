from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from trading_agent.settings import Settings, load_schedule

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("APP_MODE", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)


def test_database_url_escapes_password() -> None:
    settings = Settings(db_password=SecretStr("p@ss/word:1"))
    url = settings.database_url
    assert url.drivername == "postgresql+asyncpg"
    assert (url.host, url.port, url.database, url.username) == ("db", 5432, "trading", "agent")
    assert url.password == "p@ss/word:1"
    assert "p@ss" not in str(url)  # masked when rendered


def test_password_read_from_secrets_dir(tmp_path: Path) -> None:
    (tmp_path / "db_password").write_text("from-secret-file")
    settings = Settings(_secrets_dir=tmp_path)  # pyright: ignore[reportCallIssue]
    assert settings.db_password.get_secret_value() == "from-secret-file"


def test_password_is_required() -> None:
    with pytest.raises(ValidationError, match="db_password"):
        Settings()  # pyright: ignore[reportCallIssue]


def test_unknown_mode_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_MODE", "yolo")
    with pytest.raises(ValidationError, match="app_mode"):
        Settings(db_password=SecretStr("x"))


def test_repo_schedule_config_loads() -> None:
    schedule = load_schedule(ROOT / "config")
    assert schedule.timezone == "Europe/Berlin"
    assert 1 <= schedule.heartbeat.interval_minutes <= 60
