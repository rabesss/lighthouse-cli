"""Spawn-safe helpers for unattended renewal process tests."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from lighthouse_cli import session_renewal as sr
from lighthouse_cli.ms_errors import MicrosoftSSOError
from lighthouse_cli.session_renewal import RenewalError

WORKER_TIME = 1_800_000_015.0


class _WorkerClock:
    def __init__(self) -> None:
        self.now = WORKER_TIME

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        time.sleep(min(seconds, 0.01))


class _CountingSSO:
    def __init__(
        self,
        counter_file: Path,
        fresh_cookies: dict[str, str],
        failure_category: str | None,
        delay_seconds: float,
    ) -> None:
        self.counter_file = counter_file
        self.fresh_cookies = fresh_cookies
        self.failure_category = failure_category
        self.delay_seconds = delay_seconds
        self.provider_calls = 0

    def login(self, username: str, password: str, **kwargs: Any) -> dict[str, str]:
        provider = kwargs["totp_provider"]
        provider()
        self.provider_calls += 1
        with self.counter_file.open("a", encoding="utf-8") as counter:
            counter.write("login\n")
            counter.flush()
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if self.failure_category is not None:
            raise MicrosoftSSOError("Test sign-in failure.", category=self.failure_category)
        return self.fresh_cookies

    def close(self) -> None:
        pass


def renewal_worker(
    label: str,
    config_dir: str,
    passphrase: str,
    counter_file: str,
    observed_cookies: dict[str, str],
    fresh_cookies: dict[str, str],
    failure_category: str | None,
    delay_seconds: float,
    start_event: Any,
    result_queue: Any,
) -> None:
    """Run one renewal and report only its outcome and provider-call count."""
    os.environ["LIGHTHOUSE_CONFIG_DIR"] = config_dir
    os.environ["LIGHTHOUSE_SECRETS_PASSPHRASE"] = passphrase
    clock = _WorkerClock()
    sr._clock = clock.time
    sr._sleep = clock.sleep
    fake_sso = _CountingSSO(
        Path(counter_file), fresh_cookies, failure_category, delay_seconds
    )
    sr.MicrosoftSSOClient = lambda: fake_sso  # type: ignore[assignment]

    if start_event is not None and not start_event.wait(30):
        result_queue.put((label, "start_timeout", fake_sso.provider_calls))
        return

    def validate(cookies: dict[str, str]) -> sr.SessionProbe:
        if cookies == observed_cookies:
            return "invalid"
        if cookies == fresh_cookies:
            return "valid"
        return "unavailable"

    try:
        result = sr.renew_session(
            observed_cookies=observed_cookies,
            validate=validate,
            automatic=True,
        )
    except RenewalError as exc:
        outcome = f"blocked:{exc.category}" if failure_category is None else f"error:{exc.category}"
    except BaseException:
        outcome = "worker_error"
    else:
        outcome = "signed_in" if result.signed_in else "reused"
    result_queue.put((label, outcome, fake_sso.provider_calls))
