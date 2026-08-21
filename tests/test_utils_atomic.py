"""Tests for utils.atomic_write — the shared temp+os.replace write helper."""

from __future__ import annotations

import os
import threading

import pytest

from lighthouse_cli.utils import atomic_write


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

class TestAtomicWriteRoundTrip:
    def test_text_round_trip(self, tmp_path: os.PathLike) -> None:
        target = tmp_path / "data.txt"
        atomic_write(target, "héllo world\nsecond line\n")
        assert target.read_text(encoding="utf-8") == "héllo world\nsecond line\n"

    def test_bytes_round_trip(self, tmp_path: os.PathLike) -> None:
        target = tmp_path / "data.bin"
        payload = bytes(range(256))
        atomic_write(target, payload)
        assert target.read_bytes() == payload

    def test_overwrites_existing_target(self, tmp_path: os.PathLike) -> None:
        target = tmp_path / "data.txt"
        atomic_write(target, "v1")
        atomic_write(target, "v2")
        assert target.read_text(encoding="utf-8") == "v2"


# ---------------------------------------------------------------------------
# Permissions per mode
# ---------------------------------------------------------------------------

class TestAtomicWriteModes:
    def test_explicit_mode_applied(self, tmp_path: os.PathLike) -> None:
        target = tmp_path / "secret.txt"
        atomic_write(target, "x", mode=0o600)
        assert target.stat().st_mode & 0o777 == 0o600

    def test_assignments_semantics_stay_private(self, tmp_path: os.PathLike) -> None:
        """Attachment files keep NamedTemporaryFile's 0600 on-disk result."""
        target = tmp_path / "attachment.bin"
        atomic_write(target, b"pdf-bytes", mode=0o600)
        assert target.stat().st_mode & 0o777 == 0o600

    def test_default_mode_follows_umask(self, tmp_path: os.PathLike) -> None:
        """mode=None preserves open()'s umask default (manifest/course-config)."""
        mask = os.umask(0)
        os.umask(mask)
        target = tmp_path / "plain.json"
        atomic_write(target, "{}")
        assert target.stat().st_mode & 0o777 == 0o666 & ~mask

    def test_mode_holds_at_creation_not_just_after_write(
        self, tmp_path: os.PathLike, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0600-intended files must never be briefly world-readable: the temp
        is created WITH its final mode — spy on the os.open(..., mode) call."""
        import lighthouse_cli.utils as utils_mod

        observed: list[int] = []
        real_open = os.open

        def spy_open(path, flags, mode=0o666, **kwargs):
            if str(path).endswith(".tmp") and (flags & os.O_CREAT):
                observed.append(mode & 0o777)
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(utils_mod.os, "open", spy_open)
        atomic_write(tmp_path / "secret.txt", "payload", mode=0o600)
        assert observed == [0o600], "temp must be created with 0600 in one shot"


class TestAtomicWriteCollisionRetry:
    def test_eexist_collision_retries_with_new_temp(
        self, tmp_path: os.PathLike, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An O_EXCL collision retries with a fresh name and never unlinks
        another writer's in-flight temp."""
        import uuid as uuid_mod

        import lighthouse_cli.utils as utils_mod

        colliding = tmp_path / "target.txt.stolen.tmp"
        colliding.write_bytes(b"other writer's payload")

        real_uuid4 = uuid_mod.uuid4
        counter = {"n": 0}

        class FakeUUID:
            def __init__(self, hex_value: str) -> None:
                self.hex = hex_value

        def fake_uuid4() -> FakeUUID:
            counter["n"] += 1
            if counter["n"] == 1:
                return FakeUUID("stolen")
            return real_uuid4()

        monkeypatch.setattr(utils_mod.uuid, "uuid4", fake_uuid4)
        atomic_write(tmp_path / "target.txt", "mine", mode=0o600)

        # The other writer's temp is untouched; our data landed atomically.
        assert colliding.read_bytes() == b"other writer's payload"
        assert (tmp_path / "target.txt").read_text() == "mine"


# ---------------------------------------------------------------------------
# Failure cleanup
# ---------------------------------------------------------------------------

class TestAtomicWriteFailureCleanup:
    def test_replace_failure_leaves_target_and_no_temp(
        self, tmp_path: os.PathLike, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "keep.txt"
        atomic_write(target, "original")

        def boom(src: object, dst: object) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError, match="replace failed"):
            atomic_write(target, "updated")
        assert target.read_text(encoding="utf-8") == "original"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_missing_parent_propagates_cleanly(self, tmp_path: os.PathLike) -> None:
        target = tmp_path / "no-such-dir" / "file.txt"
        with pytest.raises(OSError):
            atomic_write(target, "x")
        assert not target.exists()


# ---------------------------------------------------------------------------
# Concurrent writers
# ---------------------------------------------------------------------------

class TestAtomicWriteConcurrency:
    def test_concurrent_writers_never_interleave_or_leave_temps(
        self, tmp_path: os.PathLike
    ) -> None:
        target = tmp_path / "contended.txt"
        payloads = [f"payload-{i:02d}-" + "x" * 5000 for i in range(8)]
        errors: list[Exception] = []

        def writer(payload: str) -> None:
            try:
                for _ in range(25):
                    atomic_write(target, payload)
            except Exception as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        content = target.read_text(encoding="utf-8")
        assert content in payloads  # one complete payload, never interleaved
        assert list(tmp_path.glob("*.tmp")) == []
