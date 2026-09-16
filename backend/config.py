"""Validated application settings, persistent encryption, and local instance context."""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken


def _comma_values(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _origin(value: str) -> str:
    if any(character.isspace() for character in value) or any(character in value for character in "\\?#"):
        raise ValueError("Allowed origins must be HTTP(S) origins without credentials or paths")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or "*" in parsed.hostname
    ):
        raise ValueError("Allowed origins must be HTTP(S) origins without credentials or paths")
    port = parsed.port  # Access validates the numeric port and its range.
    if port == 0:
        raise ValueError("Allowed origin port must be positive")
    host = parsed.hostname.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None and port != {"http": 80, "https": 443}[parsed.scheme] else ""
    return f"{parsed.scheme}://{host}{suffix}"


@dataclass(frozen=True)
class Settings:
    """Construction validates configuration without creating runtime resources."""

    data_dir: Path = Path("data")
    frontend_dir: Path = Path(__file__).resolve().parent.parent / "frontend"
    secure_cookies: bool = True
    allow_cidrs: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    session_hours: int = 12
    max_request_bytes: int = 2 * 1024 * 1024
    bootstrap_token: str | None = field(default=None, repr=False)
    docker_socket: str = '/var/run/docker.sock'

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())
        object.__setattr__(self, "frontend_dir", Path(self.frontend_dir).expanduser().resolve())
        if not isinstance(self.secure_cookies, bool):
            raise ValueError("secure_cookies must be a boolean")
        for name in ("session_hours", "max_request_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        object.__setattr__(self, "allow_cidrs", tuple(str(ipaddress.ip_network(value)) for value in self.allow_cidrs))
        object.__setattr__(self, "allowed_origins", tuple(_origin(value) for value in self.allowed_origins))
        if self.bootstrap_token is not None and not isinstance(self.bootstrap_token, str):
            raise ValueError("bootstrap_token must be a string")
        if self.bootstrap_token == "":
            object.__setattr__(self, "bootstrap_token", None)
        if (not isinstance(self.docker_socket, str) or not self.docker_socket.startswith('/')
                or self.docker_socket == '/' or any(c in self.docker_socket for c in ('\x00', '\\', '\n', '\r'))
                or any(part in ('.', '..') for part in self.docker_socket.split('/'))):
            raise ValueError('docker_socket must be an absolute Unix socket path')

    @classmethod
    def from_env(cls, data_dir: Path | None = None) -> Settings:
        """Read INFERENCE_* variables; explicit data_dir overrides the environment."""
        secure = env_value("SECURE_COOKIES", "true").strip().lower()
        if secure not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("INFERENCE_SECURE_COOKIES must be a boolean")
        return cls(
            data_dir=data_dir if data_dir is not None else Path(env_value("DATA_DIR", "data")),
            frontend_dir=Path(env_value("FRONTEND_DIR", str(cls.frontend_dir))),
            secure_cookies=secure in {"true", "1", "yes", "on"},
            allow_cidrs=_comma_values(env_value("EGRESS_ALLOW_CIDRS", "")),
            allowed_origins=_comma_values(env_value("ALLOWED_ORIGINS", "")),
            session_hours=int(env_value("SESSION_HOURS", "12")),
            max_request_bytes=int(env_value("MAX_REQUEST_BYTES", str(2 * 1024 * 1024))),
            bootstrap_token=env_value("BOOTSTRAP_TOKEN") or None,
            docker_socket=env_value('DOCKER_SOCKET', '/var/run/docker.sock'),
        )


ENV_PREFIX = "INFERENCE_"


def env_value(suffix: str, default: str | None = None) -> str | None:
    """Return INFERENCE_<suffix>, even when set but empty, else the default."""
    return os.environ.get(ENV_PREFIX + suffix, default)


def env_is_set(suffix: str) -> bool:
    return ENV_PREFIX + suffix in os.environ


class VaultError(RuntimeError):
    """A redacted failure to read or authenticate persistent secret material."""


def _regular_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise VaultError("Vault storage must use regular files, not symbolic links")


def _private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        # Best effort on filesystems without POSIX permissions; Windows needs ACLs.
        pass


def _database_has_ciphertext(path: Path) -> bool:
    if not path.exists():
        return False
    _regular_file(path)
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for table, columns in (
                ("instances", ("api_key_enc",)),
                ("users", ("totp_secret_enc", "totp_pending_enc")),
                ('approval_actions', ('state_enc',)),
                ('promotion_runs', ('state_enc',)),
                ('backup_schedules', ('config_enc',)),
            ):
                present = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                for column in columns:
                    if column in present and connection.execute(
                        f'SELECT 1 FROM "{table}" WHERE "{column}" IS NOT NULL AND "{column}" != ? LIMIT 1',
                        ("",),
                    ).fetchone():
                        return True
            document_columns = {row[1] for row in connection.execute('PRAGMA table_info(documents)')}
            if 'value_json' in document_columns and connection.execute(
                "SELECT 1 FROM documents WHERE CASE WHEN json_valid(value_json) "
                "THEN json_type(value_json,'$.encrypted') IS NOT NULL ELSE 1 END LIMIT 1"
            ).fetchone():
                return True
            return False
        finally:
            connection.close()
    except sqlite3.Error:
        raise VaultError("Cannot validate existing database before creating a vault key") from None


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REFERENCE_BODY = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*"
_REFERENCE = re.compile(r"\$\{(" + _REFERENCE_BODY + r")\}|\$(" + _REFERENCE_BODY + r")")


class Vault:
    """One lifespan-owned vault with serialized writes and a persistent Fernet key.

    secrets.json contains a versioned encrypted JSON object, including when empty.
    The empty encrypted object detects key substitution and records initialization.
    Atomic replacement protects each update; multiple application processes are
    deliberately unsupported, as they are for the application's SQLite lifecycle.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._key_path = self.data_dir / ".secret_key"
        self._secrets_path = self.data_dir / "secrets.json"
        self._lock = threading.RLock()
        _regular_file(self._key_path)
        _regular_file(self._secrets_path)
        if not self._key_path.exists():
            if self._secrets_path.exists() or _database_has_ciphertext(self.data_dir / "dashboard.db"):
                raise VaultError("Vault key is missing; restore the original key before starting")
            key = Fernet.generate_key()
            try:
                descriptor = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                # Another initializer won; never overwrite its key.
                pass
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
        try:
            _regular_file(self._key_path)
            self._fernet = Fernet(self._key_path.read_bytes())
        except (ValueError, OSError):
            raise VaultError("Vault key is unreadable or invalid; restore the original key") from None
        _private(self._key_path)
        self._values: dict[str, Any] = {}
        if self._secrets_path.exists():
            self._values = self._read()
        else:
            self._write({})
        _private(self._secrets_path)
        for filename in ("config.json", "users.json", "dashboard.db"):
            path = self.data_dir / filename
            if path.exists() and not path.is_symlink() and path.is_file():
                _private(path)

    def encrypt(self, value: str) -> str:
        """Encrypt a string for storage by this vault or another application module."""
        if not isinstance(value, str):
            raise TypeError("Vault encryption requires a string")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        """Authenticate and decrypt without exposing ciphertext in error messages."""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, AttributeError, TypeError):
            raise VaultError("Secret authentication failed; verify the original vault key and data") from None

    def _read(self) -> dict[str, Any]:
        try:
            _regular_file(self._secrets_path)
            envelope = json.loads(self._secrets_path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or envelope.get("version") != 1:
                raise ValueError
            values = json.loads(self.decrypt(envelope["ciphertext"]))
            if not isinstance(values, dict) or any(not _NAME.fullmatch(name) for name in values):
                raise ValueError
            return values
        except (ValueError, KeyError, TypeError, OSError, VaultError):
            raise VaultError("Vault data is invalid or cannot be authenticated; restore the original data and key") from None

    def _write(self, values: dict[str, Any]) -> None:
        _regular_file(self._secrets_path)
        plaintext = json.dumps(values, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        encoded = json.dumps({"version": 1, "ciphertext": self.encrypt(plaintext)}, separators=(",", ":"))
        descriptor, temporary = tempfile.mkstemp(prefix=".vault-", suffix=".tmp", dir=self.data_dir)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _private(temporary_path)
            _regular_file(self._secrets_path)
            os.replace(temporary_path, self._secrets_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def list(self) -> list[str]:
        """Return sorted secret names only, never their values."""
        with self._lock:
            return sorted(self._values)

    def set(self, name: str, value: Any) -> None:
        """Persist a JSON value; failed writes leave the current state unchanged."""
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError("Secret names must be identifiers containing letters, numbers, and underscores")
        # A round trip validates JSON and prevents callers mutating stored objects.
        owned_value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        with self._lock:
            updated = {**self._values, name: owned_value}
            self._write(updated)
            self._values = updated

    def delete(self, name: str) -> None:
        """Delete an existing secret; a missing name raises KeyError."""
        with self._lock:
            updated = self._values.copy()
            del updated[name]
            self._write(updated)
            self._values = updated

    def _reference_value(self, reference: str) -> Any:
        name, *fields = reference.split(".")
        if name in os.environ:
            value: Any = os.environ[name]
            if value.lstrip().startswith(("{", "[")):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise ValueError("Environment secret contains invalid compound JSON") from None
        else:
            with self._lock:
                if name not in self._values:
                    raise KeyError("Secret reference was not found")
                value = copy.deepcopy(self._values[name])
        for field_name in fields:
            if not isinstance(value, dict) or field_name not in value:
                raise KeyError("Secret field was not found")
            value = value[field_name]
        return value

    def resolve(self, value: str) -> Any:
        """Resolve $NAME / ${NAME.field}; environment overrides vault, even if empty.

        Whole references retain their JSON type. Embedded references must be
        scalar. Resolved secret contents are never recursively interpolated.
        """
        if not isinstance(value, str):
            raise TypeError("Secret resolution requires a string")
        match = _REFERENCE.fullmatch(value)
        if match:
            return self._reference_value(match.group(1) or match.group(2))

        def replace(match: re.Match[str]) -> str:
            resolved = self._reference_value(match.group(1) or match.group(2))
            if isinstance(resolved, (dict, list)) or resolved is None:
                raise ValueError("Compound secrets require a whole reference or an explicit scalar field")
            if isinstance(resolved, bool):
                return "true" if resolved else "false"
            return str(resolved)

        return _REFERENCE.sub(replace, value)


_instance: ContextVar[dict[str, Any] | None] = ContextVar("inference_instance", default=None)


@contextmanager
def use_instance(instance: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Temporarily bind a local context; public service operations still take IDs."""
    token = _instance.set(instance)
    try:
        yield instance
    finally:
        _instance.reset(token)


def get_instance() -> dict[str, Any]:
    """Return this context's instance, failing if none was explicitly bound."""
    instance = _instance.get()
    if instance is None:
        raise RuntimeError("No instance is bound to the current context")
    return instance
