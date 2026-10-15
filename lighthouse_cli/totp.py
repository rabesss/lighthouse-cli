"""RFC 6238 TOTP primitives for the stored authenticator (standard library only).

The CLI signs in unattended by generating the same 6-digit codes a
third-party authenticator app shows.  This module only parses the secret and
computes codes; storage lives in ``credential_store`` and the step reservation
in ``session_renewal``.

Errors never echo the supplied text, and ``repr`` never shows the key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
import urllib.parse
from dataclasses import dataclass, field

TOTP_ALGORITHM = "SHA1"
TOTP_DIGITS = 6
TOTP_PERIOD = 30

# Entra software OATH tokens use 160-bit keys.  The bounds reject truncated
# pastes and anything that is clearly not an authenticator secret.
_MIN_KEY_BYTES = 10
_MAX_KEY_BYTES = 64
_MAX_INPUT_CHARS = 2048
_BASE32_RE = re.compile(r"[A-Z2-7]+")

_INVALID_SECRET = "The authenticator secret is not valid base32."  # pragma: allowlist secret
_EMPTY_SECRET = "The authenticator secret is empty."  # pragma: allowlist secret
_BAD_KEY_LENGTH = "The authenticator secret has an unexpected length."
_INVALID_URI = "The otpauth:// URI could not be read."
_UNSUPPORTED_URI = (
    "Only otpauth://totp URIs with SHA1, 6 digits and a 30-second period are supported."
)
# Every message ``TotpSecretError`` can carry; callers may show these as-is.
SECRET_ERROR_MESSAGES = frozenset({
    _INVALID_SECRET, _EMPTY_SECRET, _BAD_KEY_LENGTH, _INVALID_URI, _UNSUPPORTED_URI,
})


class TotpSecretError(ValueError):
    """The supplied authenticator secret cannot be used.

    Messages are fixed strings; they never contain the supplied text.
    """


@dataclass(frozen=True)
class TotpParams:
    """A parsed authenticator secret.  ``repr`` never shows the key."""

    key: bytes = field(repr=False)
    digits: int = TOTP_DIGITS
    period: int = TOTP_PERIOD

    @property
    def secret_base32(self) -> str:
        """Canonical unpadded base32 form, used only for sealed storage."""
        return base64.b32encode(self.key).decode("ascii").rstrip("=")


def _decode_base32(text: str) -> bytes:
    """Decode a base32 secret, tolerating spaces, hyphens, case and padding."""
    normalized = "".join(text.split()).replace("-", "").upper().rstrip("=")
    if not normalized:
        raise TotpSecretError(_EMPTY_SECRET)
    if not _BASE32_RE.fullmatch(normalized):
        raise TotpSecretError(_INVALID_SECRET)
    try:
        key = base64.b32decode(normalized + "=" * (-len(normalized) % 8))
    except (binascii.Error, ValueError):
        raise TotpSecretError(_INVALID_SECRET) from None
    if not _MIN_KEY_BYTES <= len(key) <= _MAX_KEY_BYTES:
        raise TotpSecretError(_BAD_KEY_LENGTH)
    return key


def _secret_from_uri(text: str) -> str:
    """Return the ``secret`` parameter of an ``otpauth://totp`` URI.

    Only the parameters Entra software OATH uses are accepted; any other
    algorithm, digit count or period, an HOTP URI, or a repeated parameter is
    rejected rather than guessed at.
    """
    try:
        parts = urllib.parse.urlsplit(text)
        pairs = urllib.parse.parse_qsl(
            parts.query, keep_blank_values=True, strict_parsing=True
        )
    except ValueError:
        raise TotpSecretError(_INVALID_URI) from None
    if parts.scheme.casefold() != "otpauth" or parts.netloc.casefold() != "totp":
        raise TotpSecretError(_UNSUPPORTED_URI)
    params: dict[str, str] = {}
    for name, value in pairs:
        key = name.casefold()
        if key in params:
            raise TotpSecretError(_INVALID_URI)
        params[key] = value
    if params.get("algorithm", TOTP_ALGORITHM).upper() != TOTP_ALGORITHM:
        raise TotpSecretError(_UNSUPPORTED_URI)
    if params.get("digits", str(TOTP_DIGITS)) != str(TOTP_DIGITS):
        raise TotpSecretError(_UNSUPPORTED_URI)
    if params.get("period", str(TOTP_PERIOD)) != str(TOTP_PERIOD):
        raise TotpSecretError(_UNSUPPORTED_URI)
    if "secret" not in params:
        raise TotpSecretError(_EMPTY_SECRET)
    return params["secret"]


def parse_totp_secret(text: str) -> TotpParams:
    """Parse a bare base32 secret or an ``otpauth://totp`` URI.

    Raises:
        TotpSecretError: With a fixed message that never echoes ``text``.
    """
    if not isinstance(text, str):
        raise TotpSecretError(_INVALID_SECRET)
    text = text.strip()
    if not text:
        raise TotpSecretError(_EMPTY_SECRET)
    if len(text) > _MAX_INPUT_CHARS:
        raise TotpSecretError(_BAD_KEY_LENGTH)
    if text[:10].casefold() == "otpauth://":
        text = _secret_from_uri(text)
    return TotpParams(key=_decode_base32(text))


def time_step(for_time: float, period: int = TOTP_PERIOD) -> int:
    """Return the RFC 6238 time-step counter for a Unix timestamp."""
    return int(for_time // period)


def totp_code(
    key: bytes,
    *,
    for_time: float,
    digits: int = TOTP_DIGITS,
    period: int = TOTP_PERIOD,
) -> str:
    """Return the zero-padded RFC 6238 (HMAC-SHA1) code for ``for_time``.

    ``digits`` is 6 or 8; 8 is used only by the RFC test vectors.
    """
    if digits not in (6, 8):
        raise ValueError("Unsupported TOTP digit count.")
    counter = time_step(for_time, period)
    if counter < 0:
        raise ValueError("TOTP time is before the Unix epoch.")
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)
