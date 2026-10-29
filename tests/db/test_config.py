import pytest

from src.db import config


def test_environment_takes_precedence_and_password_is_not_interpolated(
    monkeypatch, tmp_path
):
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://file:file@localhost/file\n"
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:p%25ss%40word@localhost/env_db"
    )
    url = config.get_database_url()
    assert url.database == "env_db"
    assert url.password == "p%ss@word"
    assert url.drivername == "postgresql+psycopg"


def test_project_env_file_used(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql+psycopg://user:pass@localhost/test\n"
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert config.get_database_url().database == "test"


@pytest.mark.parametrize("value", ["", "bad-url", "sqlite:///test.db"])
def test_invalid_configuration_has_safe_error(monkeypatch, value):
    monkeypatch.setenv("DATABASE_URL", value)
    with pytest.raises(ValueError, match="DATABASE_URL") as error:
        config.get_database_url()
    assert "bad-url" not in str(error.value)
