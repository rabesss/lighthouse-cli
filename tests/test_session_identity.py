"""Targeted tests for D2L session identity ownership (config.py).

config.py is the single owner of D2L session identity: COOKIE_NAMES,
BASE_URL, COOKIE_SETTING_HOST (exact host cookies are written/filtered for),
COOKIE_EXTRACTION_DOMAINS (variants accepted when validating fresh logins),
and missing_cookie_names().
"""

from __future__ import annotations

import pytest

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.config import (
    BASE_URL,
    COOKIE_EXTRACTION_DOMAINS,
    COOKIE_NAMES,
    COOKIE_SETTING_HOST,
    missing_cookie_names,
)
from lighthouse_cli.ms_auth import MicrosoftSSOClient, MicrosoftSSOError
from lighthouse_cli import ms_errors


# ---------------------------------------------------------------------------
# Constants: roles and exact values
# ---------------------------------------------------------------------------

class TestSessionIdentityConstants:
    def test_setting_host_is_canonical_origin(self) -> None:
        """The setting host is the bare canonical hostname."""
        assert COOKIE_SETTING_HOST == "lighthouse.manipal.edu"

    def test_base_url_derived_from_setting_host(self) -> None:
        """BASE_URL shares the single owner — no second literal."""
        assert BASE_URL == f"https://{COOKIE_SETTING_HOST}"

    def test_extraction_domains_exact_variant_set(self) -> None:
        """Extraction accepts exactly today's variant set — pinned knowingly."""
        assert COOKIE_EXTRACTION_DOMAINS == (
            "lighthouse.manipal.edu",
            ".manipal.edu",
            "manipal.edu",
        )

    def test_ms_errors_no_longer_dups_identity(self) -> None:
        """The deleted duplicates stay deleted (no silent reintroduction)."""
        assert not hasattr(ms_errors, "BASE_URL")
        assert not hasattr(ms_errors, "D2L_COOKIE_NAMES")


# ---------------------------------------------------------------------------
# Write path: cookies are set on the exact setting host
# ---------------------------------------------------------------------------

class TestCookieWritePath:
    def test_apply_cookies_uses_exact_setting_host(self) -> None:
        client = LighthouseClient()
        client._apply_cookies_to_session({name: f"v-{name}" for name in COOKIE_NAMES})
        jarred = {c.name: c.domain for c in client._session.cookies}
        assert set(jarred) == set(COOKIE_NAMES)
        for domain in jarred.values():
            assert domain == COOKIE_SETTING_HOST


# ---------------------------------------------------------------------------
# Extraction: accepts exactly the configured domain variants
# ---------------------------------------------------------------------------

def _jar_with_cookies_on_domain(domain: str) -> MicrosoftSSOClient:
    client = MicrosoftSSOClient()
    for name in COOKIE_NAMES:
        client._session.cookies.set(name, f"val-{name}", domain=domain)
    return client


class TestCookieExtractionDomains:
    @pytest.mark.parametrize("domain", COOKIE_EXTRACTION_DOMAINS)
    def test_accepts_each_configured_variant(self, domain: str) -> None:
        client = _jar_with_cookies_on_domain(domain)
        try:
            cookies = client._extract_d2l_cookies()
        finally:
            client.close()
        assert cookies == {name: f"val-{name}" for name in COOKIE_NAMES}

    def test_rejects_unrelated_domain(self) -> None:
        client = _jar_with_cookies_on_domain("evil.example.com")
        try:
            with pytest.raises(MicrosoftSSOError, match="Missing required D2L cookies"):
                client._extract_d2l_cookies()
        finally:
            client.close()


# ---------------------------------------------------------------------------
# missing_cookie_names
# ---------------------------------------------------------------------------

class TestMissingCookieNames:
    def test_complete_cookies_yield_empty_list(self) -> None:
        full = {name: "value" for name in COOKIE_NAMES}
        assert missing_cookie_names(full) == []

    def test_blank_values_are_reported(self) -> None:
        partial = {name: f"v-{name}" for name in COOKIE_NAMES}
        partial[COOKIE_NAMES[0]] = "   "
        del partial[COOKIE_NAMES[1]]
        assert missing_cookie_names(partial) == [
            COOKIE_NAMES[0],
            COOKIE_NAMES[1],
        ]
