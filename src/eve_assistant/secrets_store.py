"""Encrypted storage for refresh tokens and other secrets.

Secrets are kept as one JSON document, encrypted as a whole and written to a
file outside the project folder.

- **Windows:** DPAPI, which ties the file to the current Windows login:
  copied to another machine or user, it cannot be read.
- **Elsewhere (the hosted copy on a Linux server):** Fernet from the
  cryptography library (AES-128-CBC with an HMAC-SHA256, so a changed file is
  refused, not misread). The key comes from the environment
  (EVE_ASSISTANT_SECRET_KEY, which the server keeps in a root-owned env file);
  the file is readable by its owner only. Login tokens change on every
  refresh, so the file is rewritten often: always to a temporary file first,
  then swapped in, so being killed mid-write leaves the old one whole.
"""

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV = "EVE_ASSISTANT_SECRET_KEY"
PATH_ENV = "EVE_ASSISTANT_SECRETS"  # where the encrypted file lives, off Windows


class SecretStoreError(Exception):
    pass


class SecretStore(Protocol):
    def load(self) -> dict[str, Any]: ...

    def save(self, data: dict[str, Any]) -> None: ...


def default_secrets_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "EVE Assistant" / "secrets.dpapi"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_DESCRIPTION = "EVE Assistant secrets"


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _take(blob: _DataBlob) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def dpapi_protect(plaintext: bytes) -> bytes:
    source, _keep = _blob(plaintext)
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source),
        _DESCRIPTION,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise SecretStoreError(f"DPAPI could not encrypt (Windows error {ctypes.GetLastError()}).")
    return _take(out)


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    source, _keep = _blob(ciphertext)
    out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out),
    )
    if not ok:
        raise SecretStoreError(
            "The secrets file could not be decrypted. It may have been created by another "
            "Windows user or on another machine. Log your characters in again to replace it."
        )
    return _take(out)


class DpapiFileStore:
    """SecretStore backed by a DPAPI-encrypted file. Windows only."""

    def __init__(self, path: Path | None = None):
        if sys.platform != "win32":
            raise SecretStoreError("DPAPI secret storage only works on Windows.")
        self.path = path or default_secrets_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        return json.loads(dpapi_unprotect(self.path.read_bytes()).decode("utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = dpapi_protect(json.dumps(data).encode("utf-8"))
        temp = self.path.with_suffix(".tmp")
        temp.write_bytes(encrypted)
        temp.replace(self.path)


class MemoryStore:
    """In-memory SecretStore for tests."""

    def __init__(self, data: dict[str, Any] | None = None):
        self.data = json.loads(json.dumps(data or {}))

    def load(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.data))

    def save(self, data: dict[str, Any]) -> None:
        self.data = json.loads(json.dumps(data))


def encrypted_secrets_path() -> Path:
    found = os.environ.get(PATH_ENV)
    return Path(found) if found else Path.home() / ".local" / "state" / "eve-assistant" / "secrets"


def new_key() -> str:
    """A fresh random key (32 bytes, URL-safe base64), for the server's env file."""
    return Fernet.generate_key().decode("ascii")


class EncryptedFileStore:
    """SecretStore backed by a file encrypted with a key from the environment."""

    def __init__(self, path: Path | None = None, key: str | None = None):
        key = key or os.environ.get(KEY_ENV)
        if not key:
            raise SecretStoreError(
                f"{KEY_ENV} isn't set, so login tokens can't be stored. On the server it "
                "belongs in the service's env file."
            )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except ValueError:  # includes UnicodeEncodeError
            raise SecretStoreError(
                f"{KEY_ENV} isn't a valid key: it should be 44 characters of URL-safe base64."
            ) from None
        self.path = path or encrypted_secrets_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            plain = self._fernet.decrypt(self.path.read_bytes())
        except InvalidToken:
            raise SecretStoreError(
                f"The secrets file {self.path} can't be read with this key: the key changed, "
                "or the file was altered. Log the characters in again to start a new one."
            ) from None
        return json.loads(plain.decode("utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        token = self._fernet.encrypt(json.dumps(data).encode("utf-8"))
        temp = self.path.with_name(self.path.name + ".tmp")
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(handle, "wb") as out:
            out.write(token)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temp, 0o600)  # in case a temporary file was left with other permissions
        os.replace(temp, self.path)


def default_store() -> SecretStore:
    """DPAPI on Windows; the key-encrypted file anywhere else."""
    return DpapiFileStore() if sys.platform == "win32" else EncryptedFileStore()
