"""Credential lookup order: environment, repository .env, per-user file."""

import os

from onshape_mcp import credentials

NAMES = ("ONSHAPE_ACCESS_KEY", "ONSHAPE_SECRET_KEY")


def _clear(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


def _write(path, access, secret):
    path.write_text(f"ONSHAPE_ACCESS_KEY={access}\nONSHAPE_SECRET_KEY={secret}\n")


def test_credentials_file_used_when_nothing_else_set(tmp_path, monkeypatch):
    _clear(monkeypatch)
    user_file = tmp_path / "credentials.env"
    _write(user_file, "file-access", "file-secret")
    monkeypatch.setenv("INCUTEC_CREDENTIALS_FILE", str(user_file))
    credentials.load_credentials(repo_root=tmp_path / "repo")

    assert os.environ["ONSHAPE_ACCESS_KEY"] == "file-access"
    assert os.environ["ONSHAPE_SECRET_KEY"] == "file-secret"


def test_repo_env_wins_over_credentials_file(tmp_path, monkeypatch):
    _clear(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / ".env", "repo-access", "repo-secret")
    user_file = tmp_path / "credentials.env"
    _write(user_file, "file-access", "file-secret")
    monkeypatch.setenv("INCUTEC_CREDENTIALS_FILE", str(user_file))
    credentials.load_credentials(repo_root=repo)

    assert os.environ["ONSHAPE_ACCESS_KEY"] == "repo-access"


def test_environment_wins_over_both(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ONSHAPE_ACCESS_KEY", "env-access")
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(repo / ".env", "repo-access", "repo-secret")
    user_file = tmp_path / "credentials.env"
    _write(user_file, "file-access", "file-secret")
    monkeypatch.setenv("INCUTEC_CREDENTIALS_FILE", str(user_file))
    credentials.load_credentials(repo_root=repo)

    assert os.environ["ONSHAPE_ACCESS_KEY"] == "env-access"
    assert os.environ["ONSHAPE_SECRET_KEY"] == "repo-secret"


def test_missing_files_are_skipped(tmp_path, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("INCUTEC_CREDENTIALS_FILE", str(tmp_path / "absent.env"))
    credentials.load_credentials(repo_root=tmp_path / "absent-repo")

    assert "ONSHAPE_ACCESS_KEY" not in os.environ


def test_default_credentials_path(monkeypatch):
    monkeypatch.delenv("INCUTEC_CREDENTIALS_FILE", raising=False)
    assert credentials.credentials_file() == credentials.DEFAULT_CREDENTIALS_FILE
    assert str(credentials.DEFAULT_CREDENTIALS_FILE).endswith(".config/incutec/credentials.env")
