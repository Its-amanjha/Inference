import asyncio
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def test_settings_defaults_do_not_create_runtime_files(tmp_path, monkeypatch):
    from backend.config import Settings

    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("INFERENCE_"):
            monkeypatch.delenv(name)
    settings = Settings.from_env()
    assert settings.data_dir == tmp_path / "data"
    assert settings.frontend_dir == Path(__file__).resolve().parents[1] / "frontend"
    assert settings.secure_cookies is True
    assert settings.allowed_origins == ()
    assert settings.allow_cidrs == ()
    assert settings.session_hours == 12
    assert settings.max_request_bytes == 2 * 1024 * 1024
    assert settings.bootstrap_token is None
    assert not settings.data_dir.exists()


def test_settings_environment_and_explicit_data_dir(tmp_path, monkeypatch):
    from backend.config import Settings

    monkeypatch.setenv("INFERENCE_DATA_DIR", str(tmp_path / "environment"))
    monkeypatch.setenv("INFERENCE_FRONTEND_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("INFERENCE_SECURE_COOKIES", "false")
    monkeypatch.setenv("INFERENCE_EGRESS_ALLOW_CIDRS", "10.0.0.0/8, ::1/128")
    monkeypatch.setenv("INFERENCE_ALLOWED_ORIGINS", "https://control.example, https://fleet.example:8443")
    monkeypatch.setenv("INFERENCE_SESSION_HOURS", "3")
    monkeypatch.setenv("INFERENCE_MAX_REQUEST_BYTES", "1024")
    monkeypatch.setenv("INFERENCE_BOOTSTRAP_TOKEN", "private-bootstrap-value")
    settings = Settings.from_env(data_dir=tmp_path / "explicit")
    assert settings.data_dir == tmp_path / "explicit"
    assert Settings.from_env().data_dir == tmp_path / "environment"
    assert settings.frontend_dir == tmp_path / "assets"
    assert settings.secure_cookies is False
    assert settings.allow_cidrs == ("10.0.0.0/8", "::1/128")
    assert settings.allowed_origins == ("https://control.example", "https://fleet.example:8443")
    assert settings.session_hours == 3
    assert settings.max_request_bytes == 1024
    assert settings.bootstrap_token == "private-bootstrap-value"
    assert "private-bootstrap-value" not in repr(settings)


def test_explicitly_empty_environment_values_are_used(monkeypatch):
    from backend.config import Settings

    for name in tuple(os.environ):
        if name.startswith("INFERENCE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("INFERENCE_EGRESS_ALLOW_CIDRS", "")
    monkeypatch.setenv("INFERENCE_BOOTSTRAP_TOKEN", "")
    settings = Settings.from_env()
    assert (settings.allow_cidrs, settings.bootstrap_token) == ((), None)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("INFERENCE_SECURE_COOKIES", "typo"),
        ("INFERENCE_EGRESS_ALLOW_CIDRS", "not-a-network"),
        ("INFERENCE_ALLOWED_ORIGINS", "*"),
        ("INFERENCE_ALLOWED_ORIGINS", "https://user:secret@example.com"),
        ("INFERENCE_ALLOWED_ORIGINS", "https://example.com/app"),
        ("INFERENCE_ALLOWED_ORIGINS", "https://example.com?mode=1"),
        ("INFERENCE_ALLOWED_ORIGINS", "https://example.com:99999"),
        ("INFERENCE_SESSION_HOURS", "0"),
        ("INFERENCE_SESSION_HOURS", "1.5"),
        ("INFERENCE_MAX_REQUEST_BYTES", "-1"),
    ],
)
def test_settings_reject_invalid_environment(name, value, monkeypatch):
    from backend.config import Settings

    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_vault_encrypts_values_and_keeps_key_on_restart(tmp_path):
    from backend.config import Vault

    vault = Vault(tmp_path)
    vault.set("DB", {"password": "secret-unicode-\u03bb", "port": 5432})
    vault.set("TOKEN", "private-token")
    encrypted = vault.encrypt("external-database-secret")
    key_before = (tmp_path / ".secret_key").read_bytes()
    contents = (tmp_path / "secrets.json").read_text(encoding="utf-8")
    assert "private-token" not in contents
    assert "secret-unicode" not in contents
    assert "external-database-secret" not in encrypted
    assert vault.list() == ["DB", "TOKEN"]
    restarted = Vault(tmp_path)
    assert restarted.resolve("$DB.password") == "secret-unicode-\u03bb"
    assert restarted.resolve("$DB.port") == 5432
    assert restarted.decrypt(encrypted) == "external-database-secret"
    assert (tmp_path / ".secret_key").read_bytes() == key_before
    restarted.delete("TOKEN")
    assert Vault(tmp_path).list() == ["DB"]


@pytest.mark.parametrize("damage", ["missing", "invalid", "different-valid"])
def test_key_loss_or_corruption_never_replaces_existing_data(tmp_path, damage):
    from cryptography.fernet import Fernet
    from backend.config import Vault

    vault = Vault(tmp_path)
    vault.set("DB", {"password": "persisted-secret"})
    secrets_before = (tmp_path / "secrets.json").read_bytes()
    key_path = tmp_path / ".secret_key"
    if damage == "missing":
        key_path.unlink()
    else:
        key_path.write_bytes(b"invalid-key" if damage == "invalid" else Fernet.generate_key())
    with pytest.raises(RuntimeError):
        Vault(tmp_path)
    assert (tmp_path / "secrets.json").read_bytes() == secrets_before
    if damage == "missing":
        assert not key_path.exists()


@pytest.mark.parametrize("damage", ["missing", "different-valid"])
def test_key_loss_after_all_named_secrets_deleted_still_fails_closed(tmp_path, damage):
    from cryptography.fernet import Fernet
    from backend.config import Vault

    vault = Vault(tmp_path)
    vault.encrypt("ciphertext-owned-by-another-module")
    vault.set("TEMP", "value")
    vault.delete("TEMP")
    key_path = tmp_path / ".secret_key"
    if damage == "missing":
        key_path.unlink()
    else:
        key_path.write_bytes(Fernet.generate_key())
    with pytest.raises(RuntimeError):
        Vault(tmp_path)
    if damage == "missing":
        assert not key_path.exists()


def test_existing_database_ciphertext_without_vault_key_fails_closed(tmp_path):
    from backend.config import Vault

    with sqlite3.connect(tmp_path / "dashboard.db") as connection:
        connection.execute("CREATE TABLE instances (api_key_enc TEXT)")
        connection.execute("INSERT INTO instances VALUES (?)", ("existing-ciphertext",))
    with pytest.raises(RuntimeError):
        Vault(tmp_path)
    assert not (tmp_path / ".secret_key").exists()


@pytest.mark.parametrize("contents", ["", "{broken", "[]", '{"TOKEN":12}', '{"TOKEN":"tampered-secret"}'])
def test_corrupt_vault_is_not_silently_reset(tmp_path, contents):
    from backend.config import Vault

    Vault(tmp_path)
    (tmp_path / "secrets.json").write_text(contents, encoding="utf-8")
    with pytest.raises(RuntimeError) as error:
        Vault(tmp_path)
    assert "tampered-secret" not in str(error.value)
    assert (tmp_path / "secrets.json").read_text(encoding="utf-8") == contents


def test_wrong_token_and_wrong_key_return_redacted_errors(tmp_path):
    from backend.config import Vault

    vault = Vault(tmp_path / "one")
    other = Vault(tmp_path / "two")
    for token in ("raw-sensitive-token", other.encrypt("private-value")):
        with pytest.raises(RuntimeError) as error:
            vault.decrypt(token)
        assert token not in str(error.value)
        assert "private-value" not in str(error.value)


def test_environment_precedence_and_compound_values(tmp_path, monkeypatch):
    from backend.config import Vault

    vault = Vault(tmp_path)
    vault.set("DB", {"username": "vault-user", "password": "vault-pass"})
    vault.set("TOKEN", "vault-token")
    monkeypatch.setenv("DB", '{"username":"env-user","password":"env-pass"}')
    monkeypatch.setenv("TOKEN", "")
    assert vault.resolve("$DB.password") == "env-pass"
    assert vault.resolve("${DB.username}") == "env-user"
    assert vault.resolve("$DB") == {"username": "env-user", "password": "env-pass"}
    assert vault.resolve("$TOKEN") == ""
    assert vault.resolve("postgres://${DB.username}:${DB.password}@localhost") == "postgres://env-user:env-pass@localhost"
    assert vault.resolve("literal text") == "literal text"


def test_unresolved_or_invalid_compound_reference_never_falls_back(tmp_path, monkeypatch):
    from backend.config import Vault

    vault = Vault(tmp_path)
    vault.set("DB", {"password": "must-not-fall-back"})
    monkeypatch.setenv("DB", "not-json")
    with pytest.raises((KeyError, ValueError)):
        vault.resolve("$DB.password")
    with pytest.raises(KeyError):
        vault.resolve("$UNSET_SECRET")
    monkeypatch.delenv("DB")
    with pytest.raises(KeyError):
        vault.resolve("$DB.missing")
    with pytest.raises(ValueError):
        vault.resolve("value=$DB")


@pytest.mark.parametrize("name", ["", "../escape", "BAD.NAME", "HAS SPACE", "$REFERENCE"])
def test_invalid_secret_names_do_not_change_vault(tmp_path, name):
    from backend.config import Vault

    vault = Vault(tmp_path)
    with pytest.raises(ValueError):
        vault.set(name, "private-value")
    assert Vault(tmp_path).list() == []


def test_values_must_be_valid_json_without_partial_writes(tmp_path):
    from backend.config import Vault

    vault = Vault(tmp_path)
    vault.set("TOKEN", "original")
    for value in (float("nan"), {"nested": float("inf")}, object()):
        with pytest.raises((TypeError, ValueError)):
            vault.set("TOKEN", value)
        assert vault.resolve("$TOKEN") == "original"
        assert Vault(tmp_path).resolve("$TOKEN") == "original"


def test_failed_atomic_replace_keeps_previous_disk_and_memory_state(tmp_path, monkeypatch):
    from backend import config

    vault = config.Vault(tmp_path)
    vault.set("TOKEN", "old")
    previous = (tmp_path / "secrets.json").read_bytes()

    def disk_failure(*args, **kwargs):
        raise OSError("simulated disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(config.os, "replace", disk_failure)
        with pytest.raises(OSError):
            vault.set("TOKEN", "new")
        with pytest.raises(OSError):
            vault.delete("TOKEN")
    assert vault.resolve("$TOKEN") == "old"
    assert (tmp_path / "secrets.json").read_bytes() == previous
    assert config.Vault(tmp_path).resolve("$TOKEN") == "old"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".secret_key", "secrets.json"]


def test_threaded_updates_do_not_lose_other_secret_entries(tmp_path):
    from backend.config import Vault

    vault = Vault(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: vault.set(f"KEY_{index}", index), range(20)))
    restarted = Vault(tmp_path)
    assert len(restarted.list()) == 20
    assert [restarted.resolve(f"$KEY_{index}") for index in range(20)] == list(range(20))


@pytest.mark.skipif(os.name != "posix", reason="Windows modes do not express POSIX confidentiality")
def test_key_and_vault_permissions_are_private(tmp_path):
    from backend.config import Vault

    vault = Vault(tmp_path)
    vault.set("TOKEN", "value")
    for filename in (".secret_key", "secrets.json"):
        assert stat.S_IMODE((tmp_path / filename).stat().st_mode) == 0o600


def test_symlinked_secret_paths_are_rejected(tmp_path):
    from backend.config import Vault

    outside = tmp_path / "outside"
    outside.write_text("untouched", encoding="utf-8")
    directory = tmp_path / "vault"
    directory.mkdir()
    try:
        (directory / ".secret_key").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this account")
    with pytest.raises(RuntimeError):
        Vault(directory)
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.asyncio
async def test_instance_context_isolated_across_tasks_and_reset_after_exception():
    from backend.config import get_instance, use_instance

    with pytest.raises(RuntimeError):
        get_instance()

    async def task(instance_id):
        with use_instance({"id": instance_id}):
            await asyncio.sleep(0)
            assert get_instance()["id"] == instance_id
            with pytest.raises(ValueError):
                with use_instance({"id": "nested"}):
                    assert get_instance()["id"] == "nested"
                    raise ValueError("reset")
            assert get_instance()["id"] == instance_id
        with pytest.raises(RuntimeError):
            get_instance()

    await asyncio.gather(task("source"), task("target"))
    with pytest.raises(RuntimeError):
        get_instance()
