"""Protection of HIGH-sensitivity values at rest.

DpapiCipher: Windows DPAPI (CryptProtectData) bound to the current Windows user, with application-specific entropy. The blob can only be
decrypted by the same Windows account on the same machine profile. HONEST LIMIT: it stops a copied `business.db` (backup, sync, another account)
from being readable; it does not stop malware running as the same user.

FernetFileCipher: fallback where DPAPI is unavailable (non-Windows or no pywin32): AES-based Fernet with a random key in a file next to the
database (owner-only permissions where the OS supports them). Weaker than DPAPI (the key sits beside the data) and reported as such.

`PlainCipher` exists for tests only and is never selected automatically.
"""
from __future__ import annotations

import os
from pathlib import Path

_ENTROPY = b"octg-business-brain-v1"


class CipherError(Exception):
    """A protected value could not be decrypted (different Windows user/profile, damaged data, wrong key)."""


class Cipher:
    name = "base"
    strong = False

    def encrypt(self, data: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    def decrypt(self, blob: bytes) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


class DpapiCipher(Cipher):
    name = "dpapi"
    strong = True
    PREFIX = b"DPAPI1:"

    def __init__(self) -> None:
        import win32crypt  # noqa: F401  (raises ImportError where unavailable)
        self._w = win32crypt

    def encrypt(self, data: bytes) -> bytes:
        return self.PREFIX + self._w.CryptProtectData(data, "OCTG Business Brain", _ENTROPY, None, None, 0)

    def decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(self.PREFIX):
            raise CipherError("not a DPAPI-protected value")
        try:
            return self._w.CryptUnprotectData(blob[len(self.PREFIX):], _ENTROPY, None, None, 0)[1]
        except Exception as e:  # noqa: BLE001 - pywintypes.error
            raise CipherError("could not decrypt this value (it was protected by a different Windows user or is damaged)") from e


class FernetFileCipher(Cipher):
    name = "fernet-file"
    strong = False
    PREFIX = b"FERNET1:"

    def __init__(self, key_path: Path) -> None:
        from cryptography.fernet import Fernet

        key_path = Path(key_path)
        if not key_path.is_file():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
        self._f = Fernet(key_path.read_bytes())

    def encrypt(self, data: bytes) -> bytes:
        return self.PREFIX + self._f.encrypt(data)

    def decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(self.PREFIX):
            raise CipherError("not a Fernet-protected value")
        try:
            return self._f.decrypt(blob[len(self.PREFIX):])
        except Exception as e:  # noqa: BLE001
            raise CipherError("could not decrypt this value (wrong key or damaged data)") from e


class PlainCipher(Cipher):
    """TESTS ONLY: no protection at all."""
    name = "plain"
    strong = False
    PREFIX = b"PLAIN:"

    def encrypt(self, data: bytes) -> bytes:
        return self.PREFIX + data

    def decrypt(self, blob: bytes) -> bytes:
        if not blob.startswith(self.PREFIX):
            raise CipherError("not a plain value")
        return blob[len(self.PREFIX):]


def default_cipher(data_dir: Path) -> Cipher:
    try:
        return DpapiCipher()
    except ImportError:
        return FernetFileCipher(Path(data_dir) / "business.key")
