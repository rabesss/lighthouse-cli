"""Tests for encrypted credential storage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
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


def test_save_rejects_symlinked_config_dir_without_touching_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config-directory symlink cannot redirect a credential write."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "credentials.json"
    sentinel.write_text("outside-sentinel", encoding="utf-8")
    config_link = tmp_path / "cfg"
    config_link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_link))

    with pytest.raises(CredentialStoreError) as exc_info:
        CredentialStore().save("user@manipal.edu", "secret")

    assert str(exc_info.value) == (
        "Credential storage path contains a symlink and cannot be used."
    )
    assert sentinel.read_text(encoding="utf-8") == "outside-sentinel"
    assert list(outside.iterdir()) == [sentinel]


def test_write_artifact_rejects_symlinked_target_without_touching_target(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An artifact symlink is rejected before atomic replacement."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    outside = tmp_path / "outside.json"
    outside.write_text("outside-sentinel", encoding="utf-8")
    artifact = config_dir / "credentials.json"
    artifact.symlink_to(outside)

    with pytest.raises(CredentialStoreError, match="symlink"):
        CredentialStore().write_artifact(
            artifact,
            metadata={},
            secret={"username": "user", "password": "secret"},
        )

    assert artifact.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside-sentinel"


def test_load_rejects_symlinked_artifact_without_reading_target(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Credential reads do not follow an artifact symlink outside config."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    outside = tmp_path / "outside.json"
    outside.write_text("not-a-credential-document", encoding="utf-8")
    (config_dir / "credentials.json").symlink_to(outside)

    with pytest.raises(CredentialStoreError, match="symlink"):
        CredentialStore().load()

    assert outside.read_text(encoding="utf-8") == "not-a-credential-document"


def test_legacy_migration_rejects_symlinked_config_dir_without_resealing(
    tmp_path: Path,
    fake_keyring: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy migration cannot read or replace through a config symlink."""
    from cryptography.fernet import Fernet

    outside = tmp_path / "outside"
    outside.mkdir()
    key = Fernet.generate_key()
    fake_keyring.backend.set_password(
        "lighthouse-cli", "credential-key", key.decode("ascii")
    )
    legacy = Fernet(key).encrypt(
        json.dumps({"username": "user@manipal.edu", "password": "secret"}).encode()
    )
    outside_file = outside / "credentials.json"
    outside_file.write_bytes(legacy)
    config_link = tmp_path / "cfg"
    config_link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_link))

    with pytest.raises(CredentialStoreError, match="symlink"):
        CredentialStore().load()

    assert outside_file.read_bytes() == legacy


def test_ensure_config_dir_rejects_symlink_without_chmod_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directory setup does not chmod through a config-directory symlink."""
    from lighthouse_cli.config import ensure_config_dir

    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    before_mode = outside.stat().st_mode & 0o777
    config_link = tmp_path / "cfg"
    config_link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_link))

    with pytest.raises(CredentialStoreError, match="symlink"):
        ensure_config_dir()

    assert outside.stat().st_mode & 0o777 == before_mode
    assert config_link.is_symlink()


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


@pytest.mark.parametrize(
    "secret",
    [
        {"username": {"value": "USERNAME_SENTINEL"}, "password": "pw"},
        {"username": "user@manipal.edu", "password": ["PASSWORD_SENTINEL"]},
        {"username": "", "password": "pw"},
        {"username": "user@manipal.edu", "password": ""},
    ],
)
def test_malformed_stored_credentials_raise_without_echoing_values(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret: dict[str, object],
) -> None:
    """Malformed decrypted values fail closed with a generic local error."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    store = CredentialStore()
    store.write_artifact(store.credentials_file, metadata={}, secret=secret)

    with pytest.raises(CredentialStoreError) as exc_info:
        store.load()
    message = str(exc_info.value)
    assert message == "Credentials file is corrupted."
    assert "USERNAME_SENTINEL" not in message
    assert "PASSWORD_SENTINEL" not in message


def test_non_finite_secret_data_is_rejected_without_raw_exception(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credential envelopes never persist JSON NaN/Infinity extensions."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))

    with pytest.raises(CredentialStoreError, match="malformed"):
        CredentialStore().write_artifact(
            config_dir / "credentials.json",
            metadata={},
            secret={"value": float("nan")},
        )


def test_strict_json_loader_rejects_overflowing_floats() -> None:
    from lighthouse_cli.credential_store import _loads_strict

    with pytest.raises(ValueError, match="non-finite JSON number"):
        _loads_strict('{"value": 1e999}')


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
    # 11 chars: not a multiple of 4, so b64decode(validate=True) rejects the
    # broken padding (a 12-char truncation would still decode cleanly).
    doc["kdf_salt"] = doc["kdf_salt"][:11]
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

    # iterations=1 keeps this a cache-behavior test, not 40 real PBKDF2 runs.
    for i in range(40):
        _derive_passphrase_key(f"pass-{i}", bytes([i]) * 16, 1)
    info = _derive_passphrase_key.cache_info()
    assert info.currsize <= 32


def test_passphrase_envelope_records_kdf_iterations(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sealed envelopes record their KDF count; envelopes from before the
    field existed still open via the pre-record fallback (no orphans)."""
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    CredentialStore().save("user@manipal.edu", "secret_password")
    doc = json.loads((config_dir / "credentials.json").read_text())
    assert doc["kdf_iterations"] == 600_000

    # Simulate a genuine pre-record envelope: actually seal at the legacy
    # 300k count, then strip the recorded field.
    import lighthouse_cli.credential_store as cs

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cs, "_KDF_ITERATIONS", 300_000)
        CredentialStore().save("user@manipal.edu", "secret_password")
    legacy_doc = json.loads((config_dir / "credentials.json").read_text())
    assert legacy_doc["kdf_iterations"] == 300_000
    del legacy_doc["kdf_iterations"]
    (config_dir / "credentials.json").write_text(json.dumps(legacy_doc))
    assert CredentialStore().load() == ("user@manipal.edu", "secret_password")

    legacy_doc["kdf_iterations"] = 1  # wrong count derives a wrong key: clean failure
    (config_dir / "credentials.json").write_text(json.dumps(legacy_doc))
    with pytest.raises(CredentialStoreError):
        CredentialStore().load()

@pytest.mark.parametrize("bad_iterations", [True, 0, -1, "600000", 600_001, 10**12])
def test_invalid_recorded_kdf_iterations_rejected_before_derivation(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_iterations: object,
) -> None:
    doc = _sealed_doc(config_dir, monkeypatch)
    doc["kdf_iterations"] = bad_iterations
    (config_dir / "credentials.json").write_text(json.dumps(doc))
    expected = "unsupported KDF iteration" if isinstance(
        bad_iterations, int
    ) and not isinstance(bad_iterations, bool) else "invalid KDF iteration"
    with pytest.raises(CredentialStoreError, match=expected) as exc_info:
        CredentialStore().load()
    if isinstance(bad_iterations, int) and not isinstance(bad_iterations, bool):
        assert str(bad_iterations) in str(exc_info.value)
