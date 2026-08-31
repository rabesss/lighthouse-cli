"""CredentialStore + sealed-artifact behavior tests.

Covers the key-source matrix (passphrase / keyring / neither), the versioned
envelope, recorded-provider decryption, legacy migration/compatibility policy,
cookies.json auto-upgrade gating, and the MFA checkpoint roundtrip.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from lighthouse_cli.auth import CredentialStore, CredentialStoreError
from lighthouse_cli.credential_store import FORMAT_VERSION, is_sealed_document
import lighthouse_cli.config as config_mod
from lighthouse_cli.config import (
    clear_mfa_pending,
    load_cookies,
    load_mfa_pending,
    save_cookies,
    save_mfa_pending,
    update_mfa_pending,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

PASSPHRASE_A = "matrix-passphrase-alpha"
PASSPHRASE_B = "matrix-passphrase-beta"

LEGACY_V1_PENDING = {
    "version": 1,
    "created_at": "2026-01-01T00:00:00+00:00",
    "mfa_method": "sms",
    "mfa_page_url": "https://login.microsoftonline.com/common/SAS/ProcessAuth",
    "mfa_config": {"sFT": "legacy-flow", "sCtx": "legacy-ctx"},
    "begin": {"Success": True},
    "selected_proof": {
        "auth_method_id": "OneWaySMS",
        "display": "SMS",
        "data": "+91 ***1234",
        "is_default": True,
    },
    "cookies": [{"name": "esctx", "value": "legacy-cookie", "domain": ".x", "path": "/"}],
}


@pytest.fixture
def store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "cfg"
    d.mkdir(parents=True)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(d))
    return d


def pending_path(store_dir: Path) -> Path:
    return store_dir / "mfa_pending.json"


def cookies_path(store_dir: Path) -> Path:
    return store_dir / "cookies.json"


# ---------------------------------------------------------------------------
# Key-source matrix
# ---------------------------------------------------------------------------


class TestKeySourceMatrix:
    def test_passphrase_source_seals_and_opens(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE_A)
        # Even with the keyring module completely unavailable.
        monkeypatch.setitem(sys.modules, "keyring", None)

        store = CredentialStore()
        assert store.preflight() == "passphrase"
        store.save("user@x.com", "pw-sentinel")

        doc = json.loads((store_dir / "credentials.json").read_text())
        assert doc["v"] == FORMAT_VERSION
        assert doc["key_source"] == "passphrase"
        assert "kdf_salt" in doc
        assert CredentialStore().load() == ("user@x.com", "pw-sentinel")

    def test_keyring_source_reuses_existing_entry(
        self, store_dir: Path, fake_keyring: Any
    ) -> None:
        """The pre-existing ('lighthouse-cli', 'credential-key') entry is reused,
        never replaced with a parallel entry or a raw-bytes format."""
        from cryptography.fernet import Fernet

        existing_key = Fernet.generate_key().decode()
        fake_keyring.backend.set_password("lighthouse-cli", "credential-key", existing_key)

        store = CredentialStore()
        assert store.preflight() == "keyring"
        store.save("user@x.com", "pw-sentinel")

        entry = fake_keyring.backend.get_password("lighthouse-cli", "credential-key")
        assert entry == existing_key  # read-old/write-old: untouched

        doc = json.loads((store_dir / "credentials.json").read_text())
        assert doc["key_source"] == "keyring"
        assert "kdf_salt" not in doc  # salt only applies to the passphrase KDF
        assert CredentialStore().load() == ("user@x.com", "pw-sentinel")

    def test_recorded_provider_survives_env_toggling(
        self, store_dir: Path, fake_keyring: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Data sealed under the keyring opens via the keyring even after a
        passphrase appears (and vice versa) — selection is never re-run."""
        from cryptography.fernet import Fernet

        fake_keyring.backend.set_password(
            "lighthouse-cli", "credential-key", Fernet.generate_key().decode()
        )
        CredentialStore().save("kr@x.com", "kr-pw")

        # Now a passphrase appears in the environment.
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE_A)
        assert CredentialStore().load() == ("kr@x.com", "kr-pw")

        # And data sealed under the passphrase still opens if the env stays set
        # while the keyring vanishes.
        CredentialStore().save("pp@x.com", "pp-pw")
        monkeypatch.setitem(sys.modules, "keyring", None)
        assert CredentialStore().load() == ("pp@x.com", "pp-pw")

    def test_neither_source_fails_preflight_cleanly(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
        monkeypatch.setitem(sys.modules, "keyring", None)

        with pytest.raises(CredentialStoreError) as exc_info:
            CredentialStore().preflight()
        message = str(exc_info.value)
        assert "LIGHTHOUSE_SECRETS_PASSPHRASE" in message
        assert "keyring" in message.lower()

    def test_no_side_effects_when_preflight_fails(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        cli_runner: Any,
    ) -> None:
        """auth login fails BEFORE any auth side effect (no SSO call, no writes)."""
        from unittest.mock import MagicMock, patch

        monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
        monkeypatch.setitem(sys.modules, "keyring", None)
        monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@manipal.edu")
        monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "pw-sentinel")

        mock_sso = MagicMock()
        with (
            patch("lighthouse_cli.auth.MicrosoftSSOClient", return_value=mock_sso) as sso_cls,
            patch("lighthouse_cli.auth.LighthouseClient") as client_cls,
        ):
            client_cls.return_value.check_auth.return_value = True
            result = cli_runner.invoke(
                __import__("lighthouse_cli.cli", fromlist=["cli"]).cli,
                ["auth", "login", "--json"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["success"] is False
        assert "LIGHTHOUSE_SECRETS_PASSPHRASE" in payload["error"]
        sso_cls.assert_not_called()  # no BeginAuth, no password POST
        mock_sso.login.assert_not_called()
        assert not cookies_path(store_dir).exists()
        assert not pending_path(store_dir).exists()


# ---------------------------------------------------------------------------
# Envelope + wrong-passphrase errors
# ---------------------------------------------------------------------------


class TestEnvelopeErrors:
    def test_wrong_passphrase_is_a_clean_error(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE_A)
        CredentialStore().save("user@x.com", "pw-sentinel")

        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE_B)
        with pytest.raises(CredentialStoreError) as exc_info:
            CredentialStore().load()
        message = str(exc_info.value)
        assert "Decryption failed" in message
        assert "LIGHTHOUSE_SECRETS_PASSPHRASE" in message
        # No raw cryptography internals leak into the message.
        assert "InvalidToken" not in message
        assert "Traceback" not in message

    def test_corrupted_envelope_is_a_clean_error(self, store_dir: Path) -> None:
        (store_dir / "credentials.json").write_text("{not json at all")
        with pytest.raises(CredentialStoreError):
            CredentialStore().load()


# ---------------------------------------------------------------------------
# Sealed cookies: non-auth reads + auto-upgrade gating
# ---------------------------------------------------------------------------


class TestSealedCookies:
    def test_roundtrip_and_metadata_allowlist(self, store_dir: Path) -> None:
        cookies = {"d2lSecureSessionVal": "sec-sentinel", "d2lSessionVal": "ses-sentinel"}
        save_cookies(cookies)

        doc = json.loads(cookies_path(store_dir).read_text())
        assert is_sealed_document(doc)
        assert "extracted_at" in doc
        assert "cookies" not in doc  # payload is sealed, not plaintext
        assert load_cookies() == cookies

    def test_disappearing_sealed_file_is_treated_as_absent(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_cookies({"d2lSecureSessionVal": "sec-sentinel"})
        monkeypatch.setattr(
            config_mod.CredentialStore,
            "read_artifact",
            lambda self, path: None,
        )

        assert load_cookies() == {}

    def test_wrong_passphrase_non_auth_read_warns_and_treats_as_absent(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        save_cookies({"d2lSecureSessionVal": "sec-sentinel"})
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE_B)

        loaded = load_cookies()

        assert loaded == {}
        err = capsys.readouterr().err
        assert "could not be unlocked" in err
        assert "sec-sentinel" not in err

    def test_legacy_plaintext_upgraded_only_after_preflight(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        legacy = {
            "cookies": {"d2lSecureSessionVal": "sec-sentinel"},
            "extracted_at": "2026-01-01T00:00:00+00:00",
        }
        cookies_path(store_dir).write_text(json.dumps(legacy))

        # No key source → plaintext is left untouched but fails closed.
        monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
        monkeypatch.setitem(sys.modules, "keyring", None)
        assert load_cookies() == {}
        assert json.loads(cookies_path(store_dir).read_text()) == legacy
        warning = capsys.readouterr().err
        assert "could not be sealed" in warning
        assert "sec-sentinel" not in warning

        # Key source appears → next read upgrades the file to a sealed envelope.
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE_A)
        assert load_cookies() == {"d2lSecureSessionVal": "sec-sentinel"}
        doc = json.loads(cookies_path(store_dir).read_text())
        assert is_sealed_document(doc)
        assert "sec-sentinel" not in cookies_path(store_dir).read_text()

    def test_legacy_non_string_extracted_at_not_trusted(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hostile/corrupt legacy extracted_at (non-string JSON type) is
        dropped rather than persisted verbatim — staleness math stays sane."""
        legacy = {
            "cookies": {"d2lSecureSessionVal": "sec-sentinel"},
            "extracted_at": 12345,  # truthy, but not an ISO timestamp
        }
        cookies_path(store_dir).write_text(json.dumps(legacy))
        monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE_A)

        assert load_cookies() == {"d2lSecureSessionVal": "sec-sentinel"}
        # The upgraded envelope carries a parseable ISO timestamp, so
        # get_cookie_age_days keeps working instead of silently disabling.
        doc = json.loads(cookies_path(store_dir).read_text())
        assert is_sealed_document(doc)
        assert isinstance(doc.get("extracted_at"), str)
        assert config_mod.get_cookie_age_days() is not None

    def test_cookie_age_read_from_plaintext_metadata(self, store_dir: Path) -> None:
        save_cookies({"d2lSessionVal": "ses-sentinel"})
        age = config_mod.get_cookie_age_days()
        assert age is not None and age < 1

    def test_malformed_sealed_cookie_payload_is_ignored(self, store_dir: Path) -> None:
        """A valid envelope must not make a malformed cookie object crash reads."""
        store = CredentialStore()
        store.write_artifact(
            store.cookie_file,
            metadata={"extracted_at": datetime.now(timezone.utc).isoformat()},
            secret={"cookies": None},
        )

        assert load_cookies() == {}

    def test_legacy_plaintext_upgrade_warning_drops_control_sentinels(
        self,
        store_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A failed legacy upgrade never echoes attacker-controlled metadata."""
        legacy = {
            "cookies": {"d2lSecureSessionVal": "cookie-control-sentinel"},
            "extracted_at": "2026-01-01T00:00:00+00:00\x1b[31m",
        }
        cookies_path(store_dir).write_text(json.dumps(legacy))
        monkeypatch.delenv("LIGHTHOUSE_SECRETS_PASSPHRASE", raising=False)
        monkeypatch.setitem(sys.modules, "keyring", None)

        assert load_cookies() == {}
        warning = capsys.readouterr().err
        assert "cookie-control-sentinel" not in warning
        assert "\\x1b" not in warning
        assert "\x1b" not in warning
        assert "LIGHTHOUSE_SECRETS_PASSPHRASE" in warning


# ---------------------------------------------------------------------------
# MFA pending checkpoint: compatibility policy + roundtrip matrix
# ---------------------------------------------------------------------------


class TestMfaPendingCompatibility:
    def test_legacy_v1_discarded_with_warning(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pending_path(store_dir).write_text(json.dumps(LEGACY_V1_PENDING))

        assert load_mfa_pending() is None
        assert not pending_path(store_dir).exists()
        err = capsys.readouterr().err
        assert "discarded" in err
        # The discarded file's secrets must not surface through the warning.
        assert "legacy-flow" not in err

    def test_unknown_version_cleared_and_warned(
        self, store_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pending_path(store_dir).write_text(json.dumps({"version": 99, "mfa_method": "sms"}))

        assert load_mfa_pending() is None
        assert not pending_path(store_dir).exists()
        assert "incompatible" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "document",
        [
            [],
            {"version": "VERSION_SENTINEL\n\x1b[31m"},
            {"version": {"secret": "VERSION_SECRET_SENTINEL"}},
        ],
    )
    def test_malformed_pending_documents_are_cleared_without_echo(
        self,
        store_dir: Path,
        document: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Malformed local state cannot crash or inject text into warnings."""
        pending_path(store_dir).write_text(json.dumps(document))

        assert load_mfa_pending() is None
        assert not pending_path(store_dir).exists()
        warning = capsys.readouterr().err
        assert "VERSION_SENTINEL" not in warning
        assert "VERSION_SECRET_SENTINEL" not in warning
        assert "\\x1b" not in warning
        assert "\x1b" not in warning

    def test_pending_metadata_control_values_are_not_reintroduced(
        self, store_dir: Path
    ) -> None:
        save_mfa_pending(
            {
                "created_at": "2026-01-01T00:00:00+00:00\x1b[31m",
                "mfa_method": "sms\nMETHOD_SENTINEL",
                "begin": {"Success": True},
            }
        )

        loaded = load_mfa_pending()
        assert loaded is not None
        assert "created_at" not in loaded
        assert "mfa_method" not in loaded
        assert "METHOD_SENTINEL" not in json.dumps(loaded)

    def test_legacy_v1_discard_json_purity(
        self, store_dir: Path, cli_runner: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Under --json the discard warning goes to stderr; stdout stays pure JSON."""
        from lighthouse_cli.cli import cli as root_cli

        pending_path(store_dir).write_text(json.dumps(LEGACY_V1_PENDING))
        result = cli_runner.invoke(root_cli, ["auth", "verify", "123456", "--json"], catch_exceptions=False)

        assert result.exit_code == 1
        payload = json.loads(result.stdout)  # raises if anything polluted stdout
        assert payload["success"] is False
        assert "No pending MFA session" in payload["error"]
        assert "discarded" in result.stderr


class TestMfaPendingRoundtrip:
    def _sample_payload(self) -> dict[str, Any]:
        return {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mfa_method": "sms",
            "mfa_page_url": "https://login.microsoftonline.com/common/SAS/ProcessAuth?x=1",
            "mfa_config": {"sFT": "ft-sentinel", "sCtx": "ctx-sentinel", "urlPost": "/SAS/ProcessAuth"},
            "begin": {"Success": True, "SessionId": "sid"},
            "selected_proof": {
                "auth_method_id": "OneWaySMS",
                "display": "SMS",
                "data": "+91 ***1234",
                "is_default": True,
            },
            "cookies": [{"name": "esctx", "value": "cookie-sentinel", "domain": ".x", "path": "/"}],
        }

    def test_save_load_update_clear_matrix(self, store_dir: Path) -> None:
        payload = self._sample_payload()
        save_mfa_pending(payload)

        loaded = load_mfa_pending()
        assert loaded is not None
        assert loaded["version"] == FORMAT_VERSION
        for key, value in payload.items():
            assert loaded[key] == value

        # Resumable phase 1: EndAuth success checkpoints flow tokens.
        update_mfa_pending({"end_auth_flow": "flow-sentinel", "end_auth_ctx": "ctx2-sentinel"})
        loaded = load_mfa_pending()
        assert loaded["end_auth_flow"] == "flow-sentinel"
        assert loaded["end_auth_ctx"] == "ctx2-sentinel"
        assert loaded["mfa_method"] == "sms"  # metadata preserved

        # Resumable phase 2: KMSI page checkpoint.
        update_mfa_pending({
            "kmsi_checkpoint": {"url": "https://x/kmsi", "html": "<html>kmsi</html>"},
        })
        loaded = load_mfa_pending()
        assert loaded["kmsi_checkpoint"]["url"] == "https://x/kmsi"

        clear_mfa_pending()
        assert load_mfa_pending() is None
        assert not pending_path(store_dir).exists()

    def test_disappearing_sealed_file_is_treated_as_absent(
        self, store_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        save_mfa_pending(self._sample_payload())
        monkeypatch.setattr(
            config_mod.CredentialStore,
            "read_artifact",
            lambda self, path: None,
        )

        assert load_mfa_pending() is None

    def test_on_disk_bytes_never_contain_secret_fields(self, store_dir: Path) -> None:
        save_mfa_pending(self._sample_payload())
        update_mfa_pending({"end_auth_flow": "flow-sentinel", "end_auth_ctx": "ctx2-sentinel"})

        raw = pending_path(store_dir).read_text()
        doc = json.loads(raw)
        # Plaintext allowlist only: version/created_at/mfa_method (+ envelope).
        assert doc["mfa_method"] == "sms"
        assert "created_at" in doc
        for forbidden in ("ft-sentinel", "ctx-sentinel", "cookie-sentinel", "flow-sentinel", "ctx2-sentinel"):
            assert forbidden not in raw
        for secret_key in ("mfa_config", "begin", "cookies", "end_auth_flow", "kmsi_checkpoint"):
            assert secret_key not in doc
