"""Consent/access-policy failures remain actionable without exposing upstream data."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from lighthouse_cli import auth
from lighthouse_cli.cli import cli
from lighthouse_cli.ms_auth import build_sso_error
from lighthouse_cli.ms_errors import MS_ERROR_CODES, MS_ERROR_RECOVERY, MS_INTERACTIVE_ERROR_CODES

_CASES = [
    (50131, "access or security policy", "Contact IT support"),
    (50140, "Keep me signed in", "normal browser sign-in"),
    (53000, "not compliant", "compliance requirements"),
    (53001, "domain-joined device", "device joined as required"),
    (53002, "not approved", "application approved"),
    (53003, "conditional access policy", "review the blocked sign-in"),
    (53004, "registration was blocked", "allowed MFA registration"),
    (65001, "consent is missing", "If administrator approval is required"),
    (65004, "consent was not completed", "wait for the review"),
    (90094, "Administrator consent is required", "cannot grant administrator consent"),
]


@pytest.mark.parametrize(("code", "description", "recovery"), _CASES)
def test_policy_error_uses_static_description_and_recovery(
    code: int, description: str, recovery: str,
) -> None:
    error = build_sso_error(code, "flow_token=UPSTREAM_SENTINEL", "POST credentials")

    assert description in str(error)
    assert recovery in (error.recovery or "")
    assert "UPSTREAM_SENTINEL" not in str(error)
    assert "Check your credentials" not in str(error)


def test_diagnostic_maps_cover_exactly_the_documented_interactive_codes() -> None:
    assert MS_INTERACTIVE_ERROR_CODES == {row[0] for row in _CASES}
    assert MS_INTERACTIVE_ERROR_CODES <= MS_ERROR_CODES.keys()
    assert MS_INTERACTIVE_ERROR_CODES <= MS_ERROR_RECOVERY.keys()
    assert "Terms of Use" not in MS_ERROR_CODES[50140]
    assert "administrator" not in MS_ERROR_CODES[65001].lower()


@pytest.mark.parametrize(("code", "description", "recovery"), _CASES)
@pytest.mark.parametrize("json_output", [False, True])
def test_policy_failure_keeps_cli_guidance_without_retry_or_persistence(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    description: str,
    recovery: str,
    json_output: bool,
) -> None:
    monkeypatch.setenv("LIGHTHOUSE_USERNAME", "user@example.test")
    monkeypatch.setenv("LIGHTHOUSE_PASSWORD", "password-sentinel")
    sso = MagicMock()
    sso.login.side_effect = build_sso_error(
        code, "flow_token=UPSTREAM_SENTINEL", "POST credentials",
    )
    args = ["auth", "login", "--save-credentials"]
    if json_output:
        args.append("--json")

    with (
        patch.object(auth, "MicrosoftSSOClient", return_value=sso),
        patch.object(auth, "LighthouseClient") as client,
        patch.object(auth, "save_cookies") as save_cookies,
        patch.object(auth, "clear_mfa_pending") as clear_pending,
        patch.object(auth.CredentialStore, "save") as save_credentials,
    ):
        result = CliRunner().invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == 1
    sso.login.assert_called_once()
    sso.close.assert_called_once()
    client.assert_not_called()
    save_cookies.assert_not_called()
    clear_pending.assert_not_called()
    save_credentials.assert_not_called()
    assert description in result.stderr
    assert recovery in result.stderr
    assert str(code) in result.stderr
    assert "UPSTREAM_SENTINEL" not in result.output
    assert "password-sentinel" not in result.output
    assert "Check your credentials" not in result.output
    if json_output:
        payload = json.loads(result.stdout)
        assert set(payload) == {"success", "error"}
        assert payload["success"] is False
        assert result.stderr == f"Error: {payload['error']}\n"
    else:
        assert result.stdout == ""


@pytest.mark.parametrize(("code", "_description", "_recovery"), _CASES)
def test_static_policy_guidance_survives_repeated_sanitization(
    code: int, _description: str, _recovery: str,
) -> None:
    message = auth._safe_auth_error_message(str(build_sso_error(code, None, "sign-in")))
    assert auth._safe_auth_error_message(message) == message


@pytest.mark.parametrize("code", [53005, 65002, 90095])
def test_unknown_codes_do_not_inherit_policy_or_consent_guidance(code: int) -> None:
    message = auth._safe_auth_error_message(str(build_sso_error(code, None, "sign-in")))
    assert message == f"Authentication failed ({code})."


@pytest.mark.parametrize(
    ("code", "recovery"),
    [
        (50034, "This email is not associated with a Microsoft account in this tenant."),
        (50053, "Account is temporarily locked. Wait a few minutes and try again."),
        (50055, "Your password has expired. Reset it via the Microsoft portal."),
        (50056, "Password is incorrect. If you recently changed your password, try again."),
        (50057, "Your account has been disabled. Contact IT support."),
        (50058, "Additional sign-in verification required. Check your authenticator app."),
        (50126, "Double-check your email and password. If using @manipal.edu, ensure your account is active."),
        (50133, "Password is incorrect. If you recently changed your password, try again."),
        (None, "Check your credentials and try again."),
        (99999, "Check your credentials and try again."),
    ],
)
def test_recovery_lookup_preserves_existing_non_policy_behavior(
    code: int | None, recovery: str,
) -> None:
    assert build_sso_error(code, None, "sign-in").recovery == recovery


@pytest.mark.parametrize(
    ("step", "recovery"),
    [
        ("UNTRUSTED_STEP", "Take UNTRUSTED_ACTION"),
        ("flow_token=UPSTREAM_SENTINEL", "https://example.test/?secret=UPSTREAM_SENTINEL"),
        ("sign-in", '{"password":"UPSTREAM_SENTINEL"}'),  # pragma: allowlist secret
    ],
)
def test_cli_never_copies_step_or_fix_from_a_coded_exception(
    step: str, recovery: str, capsys: pytest.CaptureFixture[str],
) -> None:
    from lighthouse_cli.ms_errors import MicrosoftSSOError

    error = MicrosoftSSOError(
        "Authentication failed: [53003] UNTRUSTED_DESCRIPTION",
        step=step,
        recovery=recovery,
    )
    assert auth._auth_error(str(error), json_output=True) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["success"] is False
    assert "UNTRUSTED" not in captured.out + captured.err
    assert "UPSTREAM_SENTINEL" not in captured.out + captured.err
    assert "example.test" not in captured.out + captured.err
    assert captured.err == f"Error: {payload['error']}\n"
