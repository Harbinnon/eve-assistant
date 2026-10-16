"""Encrypted storage for refresh tokens and other secrets.

Secrets are kept as one JSON document, encrypted as a whole and written to a
file outside the project folder. On Windows the encryption is DPAPI, which
ties the file to the current Windows login: copied to another machine or
user, it cannot be read.

Other platforms need their own backend implementing SecretStore.
"""

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Protocol


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


def default_store() -> SecretStore:
    return DpapiFileStore()
