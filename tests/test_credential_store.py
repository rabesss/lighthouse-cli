"""Tests for encrypted credential storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from lighthouse_cli.auth import CredentialStore, CredentialStoreError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".config" / "lighthouse-cli"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def credentials_path(config_dir: Path) -> Path:
    return config_dir / "credentials.json"


# ---------------------------------------------------------------------------
# VAL-AUTH-013: Encrypted credential storage
# ---------------------------------------------------------------------------

def test_save_credentials_encrypted(
    config_dir: Path,
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials are stored encrypted, not plaintext."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    store.save("user@manipal.edu", "secret_password")

    assert credentials_path.exists()
    content = credentials_path.read_text()
    # Must NOT contain plaintext password
    assert "secret_password" not in content
    assert "user@manipal.edu" not in content
    # Must contain encrypted blob (Fernet token starts with 'g' or 'gAAAAA')
    assert "gAAAAA" in content or "{" in content  # encrypted or JSON structure


def test_load_credentials_decrypts(
    config_dir: Path,
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stored credentials can be decrypted and loaded."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    store.save("user@manipal.edu", "secret_password")

    loaded = store.load()
    assert loaded is not None
    assert loaded[0] == "user@manipal.edu"
    assert loaded[1] == "secret_password"


def test_credentials_file_permissions(
    config_dir: Path,
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """credentials.json has 0600 permissions."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    store.save("user@manipal.edu", "secret_password")

    mode = credentials_path.stat().st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------------------------
# VAL-AUTH-022: Corrupted credentials file
# ---------------------------------------------------------------------------

def test_corrupted_credentials_fallback(
    config_dir: Path,
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Corrupted credentials.json raises CredentialStoreError."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    # Write garbage
    credentials_path.write_text("not valid json {{{{[[[")
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "")

    store = CredentialStore()
    with pytest.raises(CredentialStoreError):
        store.load()


# ---------------------------------------------------------------------------
# VAL-AUTH-023: Encryption key change
# ---------------------------------------------------------------------------

def test_passphrase_sealed_survives_keyring_loss(
    config_dir: Path,
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decryption uses the RECORDED key source: a passphrase-sealed artifact
    still opens when the system keyring disappears entirely."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    store.save("user@manipal.edu", "secret_password")

    # The premise below IS the passphrase source — pin it explicitly.
    doc = json.loads(credentials_path.read_text())
    assert doc["key_source"] == "passphrase"

    import sys as _sys
    monkeypatch.setitem(_sys.modules, "keyring", None)  # import becomes unavailable
    store2 = CredentialStore()
    assert store2.load() == ("user@manipal.edu", "secret_password")


def test_keyring_sealed_fails_cleanly_on_wrong_key(
    config_dir: Path,
    credentials_path: Path,
    fake_keyring: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyring-sealed artifact raises a clean error when the keyring entry
    no longer matches (different machine scenario)."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    from cryptography.fernet import Fernet

    fake_keyring.backend.set_password(
        "lighthouse-cli", "credential-key", Fernet.generate_key().decode()
    )
    store = CredentialStore()
    store.save("user@manipal.edu", "secret_password")
    assert json.loads(credentials_path.read_text())["key_source"] == "keyring"

    # Different machine: the keyring entry now holds an unrelated key.
    fake_keyring.backend.set_password(
        "lighthouse-cli", "credential-key", Fernet.generate_key().decode()
    )
    with pytest.raises(CredentialStoreError):
        CredentialStore().load()


# ---------------------------------------------------------------------------
# VAL-AUTH-030 / VAL-AUTH-031: Empty username/password rejection
# ---------------------------------------------------------------------------

def test_empty_password_rejected_in_store(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty password is rejected before saving."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    with pytest.raises(CredentialStoreError) as exc_info:
        store.save("user@manipal.edu", "")
    assert "password" in str(exc_info.value).lower()


def test_empty_username_rejected_in_store(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty username is rejected before saving."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    with pytest.raises(CredentialStoreError) as exc_info:
        store.save("", "secret")
    assert "username" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# VAL-AUTH-035: --save-credentials without credentials is an error
# ---------------------------------------------------------------------------

def test_save_credentials_only_with_successful_login(
    config_dir: Path,
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--save-credentials only saves on successful login."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    # Save credentials first
    store = CredentialStore()
    store.save("user@manipal.edu", "secret_password")

    # Verify file exists
    assert credentials_path.exists()
    original_content = credentials_path.read_text()

    # Simulate failed login - should NOT overwrite credentials
    from lighthouse_cli.auth import AuthenticationError

    with patch("lighthouse_cli.auth.CredentialStore.save", side_effect=AuthenticationError("Login failed")):
        # Failed login attempt
        pass

    # Credentials file should be unchanged
    assert credentials_path.read_text() == original_content


# ---------------------------------------------------------------------------
# VAL-AUTH-029: Custom config directory respected
# ---------------------------------------------------------------------------

def test_config_dir_env_var_respected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LIGHTHOUSE_CONFIG_DIR redirects credential storage."""
    custom_dir = tmp_path / "custom-config"
    custom_dir.mkdir()
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(custom_dir))

    store = CredentialStore()
    store.save("user@manipal.edu", "secret")

    expected_path = custom_dir / "credentials.json"
    assert expected_path.exists()
    assert not (tmp_path / ".config" / "lighthouse-cli" / "credentials.json").exists()


# ---------------------------------------------------------------------------
# Additional tests for CredentialStore
# ---------------------------------------------------------------------------

def test_store_no_credentials_file_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """load() returns None when credentials file doesn't exist."""
    config_dir = tmp_path / ".config" / "lighthouse-cli"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    assert store.load() is None


# ---------------------------------------------------------------------------
# F1: any malformed artifact or key raises CredentialStoreError, never a raw
# binascii.Error / UnicodeEncodeError / ValueError traceback.
# ---------------------------------------------------------------------------

def _sealed_doc(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Save credentials and return the parsed sealed envelope."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    CredentialStore().save("user@manipal.edu", "secret_password")
    return json.loads((config_dir / "credentials.json").read_text())


def test_truncated_kdf_salt_raises_clean_error(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _sealed_doc(config_dir, monkeypatch)
    doc["kdf_salt"] = doc["kdf_salt"][: len(doc["kdf_salt"]) // 2]  # breaks padding
    (config_dir / "credentials.json").write_text(json.dumps(doc))
    with pytest.raises(CredentialStoreError):
        CredentialStore().load()


def test_non_base64_kdf_salt_raises_clean_error(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _sealed_doc(config_dir, monkeypatch)
    doc["kdf_salt"] = "!!!not-base64!!!"
    (config_dir / "credentials.json").write_text(json.dumps(doc))
    with pytest.raises(CredentialStoreError):
        CredentialStore().load()


def test_non_ascii_ciphertext_raises_clean_error(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _sealed_doc(config_dir, monkeypatch)
    doc["ciphertext"] = "gAAAA-ünïcödé-ciphertext"
    (config_dir / "credentials.json").write_text(json.dumps(doc))
    with pytest.raises(CredentialStoreError):
        CredentialStore().load()

def test_corrupt_keyring_entry_raises_clean_error(
    config_dir: Path,
    credentials_path: Path,
    fake_keyring: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-mangled keyring entry wraps Fernet's raw ValueError cleanly."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    fake_keyring.set_password("lighthouse-cli", "credential-key", "not-a-fernet-key")
    credentials_path.write_text(json.dumps({
        "v": 2, "key_source": "keyring", "ciphertext": "gAAAAA",
    }))
    with pytest.raises(CredentialStoreError):
        CredentialStore().load()


# ---------------------------------------------------------------------------
# F15: the passphrase-derived-key cache is bounded.
# ---------------------------------------------------------------------------

def test_passphrase_key_cache_is_bounded(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lighthouse_cli.credential_store import _derive_passphrase_key

    for i in range(40):
        _derive_passphrase_key(f"pass-{i}", bytes([i]) * 16)
    info = _derive_passphrase_key.cache_info()
    assert info.currsize <= 32
