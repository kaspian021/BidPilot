"""Encrypted-at-rest secrets for ``data/settings.json``.

``settings.json`` used to contain marketplace passwords, cookies, Telegram
tokens and LLM keys as plain JSON (only protected by file mode ``0600``).  Any
backup, screen-share, log collection or accidental copy of that one file leaked
every account.  This module encrypts every secret leaf before it reaches disk
with Fernet (AES-128-CBC + HMAC-SHA256, from the maintained ``cryptography``
package — no home-grown crypto) and decrypts it transparently on read, so the
rest of the codebase keeps seeing plain values while the file on disk does not.

Key management
--------------
* ``BIDPILOT_VAULT_KEY`` (a urlsafe-base64 Fernet key) wins — the right choice
  for Docker/CI where secrets are injected by the platform.
* Otherwise a random key is generated once and stored next to the settings
  file as ``.vault.key`` with mode ``0600`` (the directory is already
  ``0700``).  Back up *both* files: without the key, encrypted values cannot
  be recovered (that is the point), and the store fails loudly instead of
  silently using ciphertext as a password.

Legacy plain values are still readable; :mod:`bidpilot.settings_store`
rewrites them encrypted on first load (automatic migration).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

#: on-disk marker for an encrypted string leaf
PREFIX = "enc:v1:"
ENV_KEY = "BIDPILOT_VAULT_KEY"
ENV_KEY_FILE = "BIDPILOT_VAULT_KEY_FILE"


class VaultError(RuntimeError):
    """The vault key is missing/corrupt or the crypto backend is unavailable."""


def _fernet(key: bytes):
    try:
        from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise VaultError(
            "بسته‌ی cryptography نصب نیست؛ ذخیره‌ی امن رمزها ممکن نیست. "
            "نصب: pip install cryptography"
        ) from exc
    try:
        return Fernet(key)
    except Exception as exc:
        raise VaultError("کلید رمزنگاری خزانه معتبر نیست (BIDPILOT_VAULT_KEY یا فایل .vault.key را بررسی کن).") from exc


def key_file_path(settings_path: str | Path | None) -> Path:
    """Where the per-install vault key lives (unless env supplies the key)."""
    override = os.environ.get(ENV_KEY_FILE)
    if override:
        return Path(override).expanduser()
    base = Path(settings_path).expanduser().parent if settings_path else Path("data")
    return base / ".vault.key"


def load_or_create_key(settings_path: str | Path | None = None) -> bytes:
    """Return the Fernet key, generating and persisting one on first use."""
    env_key = (os.environ.get(ENV_KEY) or "").strip()
    if env_key:
        return env_key.encode()

    path = key_file_path(settings_path)
    try:
        existing = path.read_bytes().strip()
    except OSError:
        existing = b""
    if existing:
        return existing

    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:  # pragma: no cover - Windows / exotic FS
            pass
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".vault-", suffix=".tmp")
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):  # pragma: no cover
                pass
            with os.fdopen(fd, "wb") as fh:
                fh.write(key + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        try:
            os.chmod(path, 0o600)
        except OSError:  # pragma: no cover
            pass
    except OSError as exc:
        raise VaultError(f"ساخت کلید رمزنگاری در {path} ناموفق بود: {exc}") from exc
    return key


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_text(plaintext: str, settings_path: str | Path | None = None) -> str:
    if is_encrypted(plaintext):
        return plaintext
    token = _fernet(load_or_create_key(settings_path)).encrypt(str(plaintext).encode("utf-8"))
    return PREFIX + token.decode("ascii")


def decrypt_text(value: str, settings_path: str | Path | None = None) -> str:
    if not is_encrypted(value):
        return value
    from cryptography.fernet import InvalidToken

    try:
        plain = _fernet(load_or_create_key(settings_path)).decrypt(value[len(PREFIX):].encode("ascii"))
    except InvalidToken as exc:
        raise VaultError(
            "رمزگشایی یکی از مقادیر محرمانه ناموفق بود؛ کلید خزانه (.vault.key یا "
            "BIDPILOT_VAULT_KEY) با فایل تنظیمات هم‌خوان نیست. هر دو فایل را از یک "
            "پشتیبان برگردان یا مقدار را دوباره وارد کن."
        ) from exc
    return plain.decode("utf-8")


def walk_secrets(values: Any, dotted: str = ""):
    """Yield ``(dotted_key, string_value)`` for every secret leaf (see settings_store)."""
    from .settings_store import is_secret_key  # local import: avoids a cycle

    if isinstance(values, dict):
        for key, item in values.items():
            child = f"{dotted}.{key}" if dotted else str(key)
            yield from walk_secrets(item, child)
    elif isinstance(values, str) and dotted and is_secret_key(dotted):
        yield dotted, values


def encrypt_secrets(values: Any, settings_path: str | Path | None = None) -> Any:
    """Deep-copy ``values`` with every secret string leaf encrypted."""
    from .settings_store import is_secret_key

    return _encrypt_node(values, "", settings_path, is_secret_key)


def _encrypt_node(node: Any, dotted: str, settings_path, is_secret_key) -> Any:
    if isinstance(node, dict):
        return {
            key: _encrypt_node(item, f"{dotted}.{key}" if dotted else str(key), settings_path, is_secret_key)
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [_encrypt_node(item, dotted, settings_path, is_secret_key) for item in node]
    if isinstance(node, str) and dotted and is_secret_key(dotted) and node:
        return encrypt_text(node, settings_path)
    return node


def decrypt_secrets(values: Any, settings_path: str | Path | None = None) -> Any:
    """Deep-copy ``values`` with every encrypted leaf decrypted (legacy plain passes through)."""
    if isinstance(values, dict):
        return {key: decrypt_secrets(item, settings_path) for key, item in values.items()}
    if isinstance(values, list):
        return [decrypt_secrets(item, settings_path) for item in values]
    if is_encrypted(values):
        return decrypt_text(values, settings_path)
    return values


def has_plaintext_secret(values: Any) -> bool:
    """True when a secret leaf is still stored unencrypted (pre-migration file)."""
    return any(not is_encrypted(value) and value for _key, value in walk_secrets(values))


__all__ = [
    "ENV_KEY",
    "ENV_KEY_FILE",
    "PREFIX",
    "VaultError",
    "decrypt_secrets",
    "decrypt_text",
    "encrypt_secrets",
    "encrypt_text",
    "has_plaintext_secret",
    "is_encrypted",
    "key_file_path",
    "load_or_create_key",
    "walk_secrets",
]
