"""Encrypted storage for every lighthouse-cli session secret.

Single owner of sealing/unsealing for ``credentials.json``, ``cookies.json``,
and the MFA pending checkpoint.  Imports nothing from ``auth``, ``ms_auth``, or
``config`` — no import cycles.

Key resolution (in order):

1. ``LIGHTHOUSE_SECRETS_PASSPHRASE`` env var → PBKDF2-HMAC-SHA256 (random salt
   stored beside the ciphertext; the salt is not secret) → Fernet key.
2. OS keyring, if importable AND a backend is available → the existing
   ``("lighthouse-cli", "credential-key")`` entry holding a Fernet-format key.
   The entry is reused as-is (read-old/write-old); a missing entry is created.
3. Neither → clean, actionable :class:`CredentialStoreError` (preflight).

Every sealed artifact records ``key_source`` ("passphrase" | "keyring") and,
for passphrase seals, the ``kdf_salt`` in its envelope header.  Decryption
ALWAYS uses the recorded provider — it never re-runs the current env-first
selection — so toggling ``LIGHTHOUSE_SECRETS_PASSPHRASE`` later cannot orphan
previously written data.

On-disk envelope (JSON)::

    {"v": 2, "key_source": "...", "kdf_salt": "...?", "ciphertext": "<Fernet token>"}

Plaintext metadata keys (e.g. ``created_at``, ``mfa_method``, ``extracted_at``)
may sit beside the envelope fields; everything secret lives inside the
ciphertext.  The key itself is never stored on disk beside the ciphertext.

Legacy formats are migrated on first successful read: raw-Fernet
``credentials.json`` re-seals under the keyring source; plaintext
``cookies.json`` re-seals via the current resolution order.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from lighthouse_cli.utils import atomic_write

SERVICE_NAME = "lighthouse-cli"
KEY_NAME = "credential-key"
PASSPHRASE_ENV = "LIGHTHOUSE_SECRETS_PASSPHRASE"
CONFIG_DIR_ENV = "LIGHTHOUSE_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "~/.config/lighthouse-cli"

FORMAT_VERSION = 2
_KDF_ITERATIONS = 600_000
#: Iterations assumed for envelopes written before the count was recorded
#: (sealed at 300,000). Changing ``_KDF_ITERATIONS`` no longer orphans them.
_LEGACY_KDF_ITERATIONS = 300_000
_SUPPORTED_KDF_ITERATIONS = frozenset({_LEGACY_KDF_ITERATIONS, _KDF_ITERATIONS})

#: Envelope header keys — never treated as artifact metadata.
_ENVELOPE_KEYS = frozenset({"v", "key_source", "kdf_salt", "kdf_iterations", "ciphertext"})


class CredentialStoreError(Exception):
    """Raised when key resolution, sealing, or unsealing fails.

    Messages are always actionable and never contain secret material or raw
    cryptography tracebacks.
    """


# ---------------------------------------------------------------------------
# Key sources
# ---------------------------------------------------------------------------


def _passphrase_from_env() -> str | None:
    value = os.getenv(PASSPHRASE_ENV)
    if value is None or value == "":
        return None
    return value


def _load_keyring_module() -> Any | None:
    """Import keyring, or None when unavailable (missing or broken install)."""
    try:
        import keyring
    except Exception:
        return None
    return keyring


def _keyring_backend_available(keyring_mod: Any) -> bool:
    """True when ``keyring`` resolves a usable backend (priority > 0)."""
    try:
        backend = keyring_mod.get_keyring()
    except Exception:
        return False
    if backend is None:
        return False
    try:
        return int(getattr(backend, "priority", 1)) > 0
    except (TypeError, ValueError):
        return True


@lru_cache(maxsize=32)
def _derive_passphrase_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    """PBKDF2-HMAC-SHA256 → urlsafe base64 Fernet key (LRU-capped at 32).

    ``iterations`` is part of the cache key: envelopes sealed at different
    counts must never share a derived key.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _fernet_from_key(key: bytes) -> Fernet:
    """Wrap ``key`` in Fernet; malformed keys raise a clean error (F1)."""
    try:
        return Fernet(key)
    except Exception as exc:
        raise CredentialStoreError(
            "The stored encryption key is malformed; sealed data cannot be "
            f"opened ({exc.__class__.__name__}). Check {PASSPHRASE_ENV} or "
            "your system keyring."
        ) from None


def _decode_kdf_salt(salt_b64: str) -> bytes:
    """Decode an envelope's KDF salt; corruption raises a clean error (F1)."""
    try:
        salt = base64.b64decode(salt_b64, validate=True)
    except Exception:
        raise CredentialStoreError(
            "Sealed data has an invalid KDF salt and cannot be opened."
        ) from None
    if not salt:
        raise CredentialStoreError(
            "Sealed data has an invalid KDF salt and cannot be opened."
        )
    return salt


def _keyring_read_entry(keyring_mod: Any) -> str | None:
    """Read the existing keyring entry; None when absent."""
    try:
        return keyring_mod.get_password(SERVICE_NAME, KEY_NAME)
    except Exception as exc:
        raise CredentialStoreError(
            "System keyring is not readable "
            f"({exc.__class__.__name__}). Unlock your keyring or set "
            f"{PASSPHRASE_ENV} instead."
        ) from None


def _keyring_key(*, create: bool) -> bytes:
    """Return the keyring-held Fernet key, creating the entry when allowed."""
    keyring_mod = _load_keyring_module()
    if keyring_mod is None:
        raise CredentialStoreError(
            "The keyring package is not installed. Install it with "
            "'pip install keyring' or set "
            f"{PASSPHRASE_ENV} to seal session secrets."
        )
    if not _keyring_backend_available(keyring_mod):
        raise CredentialStoreError(_NO_KEY_SOURCE_MSG)

    stored = _keyring_read_entry(keyring_mod)
    if stored:
        return stored.encode("utf-8")
    if not create:
        raise CredentialStoreError(
            f"Keyring entry ('{SERVICE_NAME}', '{KEY_NAME}') is missing. "
            f"Set {PASSPHRASE_ENV} or re-run the command that seals the data."
        )
    key = Fernet.generate_key()
    try:
        keyring_mod.set_password(SERVICE_NAME, KEY_NAME, key.decode("ascii"))
    except Exception as exc:
        raise CredentialStoreError(
            "Could not store the encryption key in the system keyring "
            f"({exc.__class__.__name__}). Unlock your keyring or set "
            f"{PASSPHRASE_ENV} instead."
        ) from None
    return key


_NO_KEY_SOURCE_MSG = (
    "No encryption key source is available. Set "
    f"{PASSPHRASE_ENV} (recommended for headless/cron use) or install and "
    "unlock a system keyring backend (pip install keyring)."
)


# ---------------------------------------------------------------------------
# CredentialStore
# ---------------------------------------------------------------------------


class CredentialStore:
    """Seals and opens every session-secret artifact under a resolved key.

    Artifacts (all inside the config directory, mode ``0600``):

    - ``credentials.json`` — stored username/password
    - ``cookies.json`` — D2L session cookies
    - ``mfa_pending.json`` — in-progress MFA checkpoint

    The config directory is resolved from ``LIGHTHOUSE_CONFIG_DIR`` at
    construction time.
    """

    SERVICE_NAME = SERVICE_NAME
    KEY_NAME = KEY_NAME

    def __init__(self) -> None:
        self.config_dir = Path(
            os.getenv(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR)
        ).expanduser()
        self.credentials_file = self.config_dir / "credentials.json"
        self.cookie_file = self.config_dir / "cookies.json"
        self.mfa_pending_file = self.config_dir / "mfa_pending.json"

    # -- preflight ------------------------------------------------------------

    def preflight(self) -> str:
        """Resolve a usable key source BEFORE any auth side effects.

        Returns the key source that sealing will use ("passphrase" or
        "keyring").  For the keyring source the entry is created eagerly when
        missing, so a locked/absent keyring fails here — not mid-login.

        Raises:
            CredentialStoreError: With an actionable message when neither key
                source is available.
        """
        if _passphrase_from_env() is not None:
            return "passphrase"
        keyring_mod = _load_keyring_module()
        if keyring_mod is not None and _keyring_backend_available(keyring_mod):
            _keyring_key(create=True)
            return "keyring"
        raise CredentialStoreError(_NO_KEY_SOURCE_MSG)

    # -- envelope primitives ---------------------------------------------------

    def open_bytes(self, blob: bytes) -> bytes:
        """Open a sealed envelope, decrypting with the RECORDED key source."""
        envelope = _parse_envelope(blob)
        return self._open_envelope(envelope)

    # -- artifact documents ----------------------------------------------------

    def write_artifact(
        self,
        path: Path,
        *,
        metadata: dict[str, Any],
        secret: dict[str, Any],
        mode: int = 0o600,
    ) -> None:
        """Atomically write ``metadata`` (plaintext) + sealed ``secret``."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.config_dir.chmod(0o700)
        except OSError:
            pass
        doc = self._build_document(metadata, json.dumps(secret).encode("utf-8"))
        atomic_write(path, doc, mode=mode)

    def read_artifact(self, path: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Read a sealed artifact.

        Returns:
            ``(metadata, secret)`` or None when the file does not exist.

        Raises:
            CredentialStoreError: When the document is malformed or cannot be
                decrypted with its recorded key source.
        """
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise CredentialStoreError(
                f"{path.name} is corrupted ({exc.__class__.__name__})."
            ) from None
        if not isinstance(doc, dict):
            raise CredentialStoreError(f"{path.name} is corrupted.")
        metadata = {k: v for k, v in doc.items() if k not in _ENVELOPE_KEYS}
        plaintext = self._open_envelope(doc)
        try:
            secret = json.loads(plaintext.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CredentialStoreError(
                f"{path.name} decrypted to invalid data ({exc.__class__.__name__})."
            ) from None
        if not isinstance(secret, dict):
            raise CredentialStoreError(f"{path.name} decrypted to invalid data.")
        return metadata, secret

    # -- stored credentials (existing public API) -------------------------------

    def save(self, username: str, password: str) -> None:
        """Encrypt and save credentials to disk.

        Raises:
            CredentialStoreError: If credentials are empty or sealing fails.
        """
        if not username or not username.strip():
            raise CredentialStoreError("Username cannot be empty")
        if not password or not password.strip():
            raise CredentialStoreError("Password cannot be empty")

        self.write_artifact(
            self.credentials_file,
            metadata={},
            secret={"username": username, "password": password},
        )

    def load(self) -> tuple[str, str] | None:
        """Load and decrypt stored credentials.

        Returns:
            ``(username, password)`` or None when no credentials are stored.

        Raises:
            CredentialStoreError: When decryption fails or the key source for
                the recorded/legacy format is unavailable.
        """
        try:
            if not self.credentials_file.exists():
                return None
            blob = self.credentials_file.read_bytes()
        except OSError as exc:
            raise CredentialStoreError(
                f"{self.credentials_file.name} could not be read "
                f"({exc.__class__.__name__})."
            ) from None
        if not blob.lstrip().startswith(b"{"):
            return self._load_and_migrate_legacy(blob)
        try:
            data = json.loads(self.open_bytes(blob).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CredentialStoreError(
                f"Credentials file is corrupted ({exc.__class__.__name__})."
            ) from None
        if not isinstance(data, dict):
            raise CredentialStoreError("Credentials file is corrupted.")
        return data.get("username", ""), data.get("password", "")

    def _load_and_migrate_legacy(self, blob: bytes) -> tuple[str, str]:
        """Decrypt legacy raw-Fernet credentials with the keyring key, then
        re-seal them as a v2 envelope recording the keyring source.

        The legacy file is left untouched when the keyring is unavailable.
        """
        key = _keyring_key(create=False)
        try:
            plaintext = _fernet_from_key(key).decrypt(blob)
            data = json.loads(plaintext.decode("utf-8"))
        except InvalidToken:
            raise CredentialStoreError(
                "Stored credentials could not be decrypted — the keyring key "
                "does not match. Re-run 'lighthouse auth login "
                "--save-credentials' to replace them."
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CredentialStoreError(
                f"Credentials file is corrupted ({exc.__class__.__name__})."
            ) from None

        if not isinstance(data, dict):
            raise CredentialStoreError("Credentials file is corrupted.")
        username, password = data.get("username", ""), data.get("password", "")
        # Migration re-seals under the provider that just decrypted the data —
        # never the current env-first selection.
        doc = self._build_document(
            {}, plaintext, force_source="keyring",
        )
        atomic_write(self.credentials_file, doc, mode=0o600)
        return username, password

    # -- internals --------------------------------------------------------------

    def _select_source(self) -> str:
        if _passphrase_from_env() is not None:
            return "passphrase"
        keyring_mod = _load_keyring_module()
        if keyring_mod is not None and _keyring_backend_available(keyring_mod):
            return "keyring"
        raise CredentialStoreError(_NO_KEY_SOURCE_MSG)

    def _build_document(
        self,
        metadata: dict[str, Any],
        plaintext: bytes,
        *,
        force_source: str | None = None,
    ) -> bytes:
        source = force_source or self._select_source()
        doc: dict[str, Any] = dict(metadata)
        doc["v"] = FORMAT_VERSION
        doc["key_source"] = source
        if source == "passphrase":
            passphrase = _passphrase_from_env()
            if passphrase is None:
                raise CredentialStoreError(
                    f"{PASSPHRASE_ENV} is not set; cannot seal with the "
                    "passphrase key source."
                )
            salt = os.urandom(16)
            doc["kdf_salt"] = base64.b64encode(salt).decode("ascii")
            doc["kdf_iterations"] = _KDF_ITERATIONS
            fernet = _fernet_from_key(
                _derive_passphrase_key(passphrase, salt, _KDF_ITERATIONS)
            )
        else:
            fernet = _fernet_from_key(_keyring_key(create=True))
        doc["ciphertext"] = fernet.encrypt(plaintext).decode("ascii")
        return json.dumps(doc, indent=2).encode("utf-8")

    def _open_envelope(self, envelope: dict[str, Any]) -> bytes:
        """Decrypt a parsed envelope; every failure is a clean error.

        Corrupted salts, malformed keys, non-ascii ciphertext, and raw
        cryptography errors all surface as :class:`CredentialStoreError` —
        never as tracebacks (binascii/Unicode/ValueError).
        """
        source = envelope.get("key_source")
        try:
            if source == "passphrase":
                salt_b64 = envelope.get("kdf_salt")
                if not isinstance(salt_b64, str):
                    raise CredentialStoreError(
                        "Sealed data is missing its KDF salt and cannot be opened."
                    )
                passphrase = _passphrase_from_env()
                if passphrase is None:
                    raise CredentialStoreError(
                        f"This data was sealed with {PASSPHRASE_ENV}; set the same "
                        "passphrase to decrypt it."
                    )
                if "kdf_iterations" not in envelope:
                    iterations = _LEGACY_KDF_ITERATIONS
                else:
                    raw_iterations = envelope.get("kdf_iterations")
                    if not isinstance(raw_iterations, int) or isinstance(
                        raw_iterations, bool
                    ):
                        raise CredentialStoreError(
                            "Sealed data records an invalid KDF iteration count "
                            "and cannot be opened."
                        )
                    if raw_iterations not in _SUPPORTED_KDF_ITERATIONS:
                        raise CredentialStoreError(
                            "Sealed data records unsupported KDF iteration count "
                            f"{raw_iterations} and cannot be opened."
                        )
                    iterations = raw_iterations
                key = _derive_passphrase_key(
                    passphrase, _decode_kdf_salt(salt_b64), iterations
                )
            elif source == "keyring":
                key = _keyring_key(create=False)
            else:
                raise CredentialStoreError(
                    "Sealed data records an unknown key source and cannot be opened."
                )
            fernet = _fernet_from_key(key)
            ciphertext = envelope.get("ciphertext")
            if not isinstance(ciphertext, str):
                raise CredentialStoreError(
                    "Sealed data has no readable ciphertext and cannot be opened."
                )
            return fernet.decrypt(ciphertext.encode("ascii"))
        except InvalidToken:
            raise CredentialStoreError(
                "Decryption failed — the data was sealed with a different key. "
                f"Check {PASSPHRASE_ENV} or your system keyring."
            ) from None
        except (binascii.Error, UnicodeError, ValueError) as exc:
            raise CredentialStoreError(
                "Sealed data is corrupted "
                f"({exc.__class__.__name__}) and cannot be opened."
            ) from None


def _parse_envelope(blob: bytes) -> dict[str, Any]:
    """Validate a sealed envelope's shape; raise cleanly when malformed."""
    try:
        envelope = json.loads(blob.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CredentialStoreError(
            f"Sealed data is corrupted ({exc.__class__.__name__})."
        ) from None
    if (
        not isinstance(envelope, dict)
        or envelope.get("v") != FORMAT_VERSION
        or not isinstance(envelope.get("ciphertext"), str)
    ):
        raise CredentialStoreError(
            f"Unsupported sealed-data format; expected v{FORMAT_VERSION} envelope."
        )
    return envelope


def is_sealed_document(data: object) -> bool:
    """True when ``data`` looks like a v2 sealed document."""
    return (
        isinstance(data, dict)
        and data.get("v") == FORMAT_VERSION
        and isinstance(data.get("ciphertext"), str)
    )
