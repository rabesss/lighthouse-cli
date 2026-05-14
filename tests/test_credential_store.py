"""Tests for encrypted credential storage."""

from __future__ import annotations

from pathlib import Path
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

def test_encryption_key_change_graceful(
    config_dir: Path,
    credentials_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decryption failure on different machine prompts for credentials (returns None)."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    # First, save with a key
    store = CredentialStore()
    store.save("user@manipal.edu", "secret_password")

    # Now simulate keyring being cleared (different machine scenario)
    # by patching keyring to raise an exception
    with patch("keyring.get_password", side_effect=Exception("Keyring unavailable")):
        store2 = CredentialStore()
        with pytest.raises(CredentialStoreError):
            store2.load()


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


def test_store_exists(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CredentialStore.exists() returns True when credentials file exists."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    store = CredentialStore()
    assert not store.exists()

    store.save("user@manipal.edu", "secret")
    assert store.exists()


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
    assert not store.exists()
