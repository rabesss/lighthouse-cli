"""Real spawn-process coverage for unattended session renewal."""

from __future__ import annotations

import json
import multiprocessing
import queue
import time
from pathlib import Path
from typing import Any

import pytest

from lighthouse_cli.config import load_cookies, save_cookies
from lighthouse_cli.credential_store import CredentialStore
from lighthouse_cli.totp import parse_totp_secret
from tests._relogin_mp_helper import WORKER_TIME, renewal_worker

USERNAME = "worker@example.test"
PASSWORD = "test-password-only"  # pragma: allowlist secret
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # pragma: allowlist secret
PASSPHRASE = "test-passphrase-only"
EXPIRED_COOKIES = {
    "d2lSameSiteCanaryA": "canary-a-expired",
    "d2lSameSiteCanaryB": "canary-b-expired",
    "d2lSecureSessionVal": "secure-expired",
    "d2lSessionVal": "session-expired",
}
FRESH_COOKIES = {
    "d2lSameSiteCanaryA": "canary-a-fresh",
    "d2lSameSiteCanaryB": "canary-b-fresh",
    "d2lSecureSessionVal": "secure-fresh",
    "d2lSessionVal": "session-fresh",
}

WorkerSpec = tuple[
    str,
    str,
    str,
    str,
    dict[str, str],
    dict[str, str],
    str | None,
    float,
]
WorkerResult = tuple[str, str, int]


def _seed_store(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> CredentialStore:
    config_dir.mkdir(mode=0o700)
    monkeypatch.setenv("LIGHTHOUSE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LIGHTHOUSE_SECRETS_PASSPHRASE", PASSPHRASE)
    store = CredentialStore(config_dir=config_dir)
    store.save(USERNAME, PASSWORD)
    store.save_totp(USERNAME, parse_totp_secret(TOTP_SECRET), last_step=None)
    save_cookies(EXPIRED_COOKIES)
    return store


def _worker_spec(
    label: str,
    config_dir: Path,
    counter_file: Path,
    *,
    failure_category: str | None = None,
    delay_seconds: float = 0.0,
) -> WorkerSpec:
    return (
        label,
        str(config_dir),
        PASSPHRASE,
        str(counter_file),
        EXPIRED_COOKIES,
        FRESH_COOKIES,
        failure_category,
        delay_seconds,
    )


def _run_workers(
    specs: list[WorkerSpec], *, start_together: bool
) -> dict[str, tuple[str, int]]:
    context = multiprocessing.get_context("spawn")
    result_queue: Any = context.Queue()
    start_event: Any = context.Event() if start_together else None
    processes: list[Any] = []
    deadline = time.monotonic() + 60
    results: list[WorkerResult] = []

    try:
        for spec in specs:
            process = context.Process(
                target=renewal_worker,
                args=(*spec, start_event, result_queue),
            )
            process.start()
            processes.append(process)
        if start_event is not None:
            start_event.set()

        while len(results) < len(specs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail("Timed out after 60 seconds waiting for renewal worker results.")
            try:
                results.append(result_queue.get(timeout=min(1.0, remaining)))
            except queue.Empty:
                failed = [process for process in processes if process.exitcode not in (None, 0)]
                if failed:
                    pytest.fail("A spawned renewal worker exited before reporting its outcome.")

        for process in processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                pytest.fail("A spawned renewal worker did not exit within 60 seconds.")
            assert process.exitcode == 0, "A spawned renewal worker exited unsuccessfully."

        assert len(results) == len(specs)
        assert len({label for label, _, _ in results}) == len(specs)
        return {label: (outcome, provider_calls) for label, outcome, provider_calls in results}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        result_queue.close()
        result_queue.join_thread()


def test_two_processes_share_one_unattended_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    _seed_store(config_dir, monkeypatch)
    counter_file = tmp_path / "login-count.txt"
    results = _run_workers(
        [
            _worker_spec("worker-a", config_dir, counter_file, delay_seconds=0.5),
            _worker_spec("worker-b", config_dir, counter_file, delay_seconds=0.5),
        ],
        start_together=True,
    )

    assert sorted(outcome for outcome, _ in results.values()) == ["reused", "signed_in"]
    assert sum(provider_calls for _, provider_calls in results.values()) == 1
    # Only the process that signs in prints the notice; the other reuses its cookies.
    assert counter_file.read_text(encoding="utf-8").splitlines() == ["notice", "login"]
    assert load_cookies(read_only=True) == FRESH_COOKIES


def test_blocked_breaker_prevents_second_process_and_totp_step_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    store = _seed_store(config_dir, monkeypatch)
    counter_file = tmp_path / "login-count.txt"

    first = _run_workers(
        [
            _worker_spec(
                "failing-worker",
                config_dir,
                counter_file,
                failure_category="code_rejected",
            )
        ],
        start_together=False,
    )
    assert first == {"failing-worker": ("error:code_rejected", 1)}

    second = _run_workers(
        [_worker_spec("blocked-worker", config_dir, counter_file)],
        start_together=False,
    )
    assert second == {"blocked-worker": ("blocked:code_rejected", 0)}
    assert counter_file.read_text(encoding="utf-8").splitlines() == ["notice", "login"]

    state = json.loads((config_dir / "relogin_state.json").read_text(encoding="utf-8"))
    assert state["blocked"] is True
    assert state["category"] == "code_rejected"
    stored = store.load_totp()
    assert stored is not None
    assert stored.last_step == int(WORKER_TIME // stored.params.period)
