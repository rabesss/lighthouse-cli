"""RFC 6238 TOTP primitives: vectors, secret parsing and secret-safe errors."""

from __future__ import annotations

import base64

import pytest

from lighthouse_cli.totp import (
    TotpParams,
    TotpSecretError,
    parse_totp_secret,
    time_step,
    totp_code,
)

# RFC 6238 Appendix B, SHA1 seed.
_RFC_KEY = b"12345678901234567890"
_RFC_BASE32 = base64.b32encode(_RFC_KEY).decode("ascii")
_SENTINEL_KEY = b"sentinel-key-material-20b"
_SENTINEL_BASE32 = base64.b32encode(_SENTINEL_KEY).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    ("for_time", "expected"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_rfc6238_sha1_vectors(for_time: int, expected: str) -> None:
    assert totp_code(_RFC_KEY, for_time=for_time, digits=8) == expected


@pytest.mark.parametrize(
    ("counter", "expected"),
    [(0, "755224"), (1, "287082"), (2, "359152"), (3, "969429"), (9, "520489")],
)
def test_six_digit_truncation_matches_rfc4226(counter: int, expected: str) -> None:
    assert totp_code(_RFC_KEY, for_time=counter * 30 + 29.9) == expected


def test_time_step_floors_to_period() -> None:
    assert time_step(59.999) == 1
    assert time_step(60) == 2
    assert time_step(0) == 0


def test_rejects_unsupported_digits_and_pre_epoch_time() -> None:
    with pytest.raises(ValueError):
        totp_code(_RFC_KEY, for_time=0, digits=7)
    with pytest.raises(ValueError):
        totp_code(_RFC_KEY, for_time=-31)


@pytest.mark.parametrize(
    "text",
    [
        _RFC_BASE32,
        _RFC_BASE32.lower(),
        _RFC_BASE32.rstrip("="),
        " ".join(_RFC_BASE32[i:i + 4] for i in range(0, 32, 4)),
        "-".join(_RFC_BASE32[i:i + 4] for i in range(0, 32, 4)),
        f"  {_RFC_BASE32}\n",
    ],
)
def test_bare_base32_is_normalized(text: str) -> None:
    params = parse_totp_secret(text)
    assert params.key == _RFC_KEY
    assert (params.digits, params.period) == (6, 30)
    assert params.secret_base32 == _RFC_BASE32.rstrip("=")


@pytest.mark.parametrize(
    "uri",
    [
        f"otpauth://totp/MAHE:student?secret={_RFC_BASE32}",
        f"otpauth://totp/Label?secret={_RFC_BASE32}&issuer=Microsoft",
        f"OTPAUTH://TOTP/Label?secret={_RFC_BASE32}&algorithm=sha1&digits=6&period=30",
    ],
)
def test_otpauth_uri_is_accepted(uri: str) -> None:
    assert parse_totp_secret(uri).key == _RFC_KEY


@pytest.mark.parametrize(
    "uri",
    [
        f"otpauth://hotp/Label?secret={_RFC_BASE32}&counter=1",
        f"otpauth://totp/Label?secret={_RFC_BASE32}&algorithm=SHA256",
        f"otpauth://totp/Label?secret={_RFC_BASE32}&digits=8",
        f"otpauth://totp/Label?secret={_RFC_BASE32}&period=60",
        f"otpauth://totp/Label?secret={_RFC_BASE32}&secret={_RFC_BASE32}",
        f"otpauth://totp/Label?secret={_RFC_BASE32}&Digits=6&digits=6",
        "otpauth://totp/Label?issuer=Microsoft",
        "otpauth://totp/Label?secret",
    ],
)
def test_otpauth_uri_rejections(uri: str) -> None:
    with pytest.raises(TotpSecretError):
        parse_totp_secret(uri)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not base32 at all!",
        "ABC1",  # '1' is not in the base32 alphabet
        "A",  # impossible base32 length
        base64.b32encode(b"short").decode("ascii"),
        base64.b32encode(b"x" * 65).decode("ascii"),
        "A" * 3000,
    ],
)
def test_invalid_secrets_are_rejected(text: str) -> None:
    with pytest.raises(TotpSecretError):
        parse_totp_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        f"{_SENTINEL_BASE32}!",
        f"otpauth://totp/student@example.edu?secret={_SENTINEL_BASE32}&digits=8",
        f"otpauth://hotp/student@example.edu?secret={_SENTINEL_BASE32}",
        f"otpauth://totp/x?secret={_SENTINEL_BASE32}&secret={_SENTINEL_BASE32}",
        f"otpauth://totp/x?secret={_SENTINEL_BASE32}&bad",
    ],
)
def test_errors_never_echo_the_input(text: str) -> None:
    with pytest.raises(TotpSecretError) as caught:
        parse_totp_secret(text)
    rendered = f"{caught.value!s} {caught.value!r}"
    assert _SENTINEL_BASE32 not in rendered
    assert _SENTINEL_BASE32[:12] not in rendered
    assert "student@example.edu" not in rendered


def test_repr_hides_the_key() -> None:
    params = parse_totp_secret(_SENTINEL_BASE32)
    rendered = repr(params)
    assert "key" not in rendered
    assert _SENTINEL_BASE32 not in rendered
    assert repr(_SENTINEL_KEY) not in rendered
    assert rendered == "TotpParams(digits=6, period=30)"


def test_params_are_frozen() -> None:
    params = TotpParams(key=_RFC_KEY)
    with pytest.raises(AttributeError):
        params.key = b"other"  # type: ignore[misc]
