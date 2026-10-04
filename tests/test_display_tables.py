"""Table rendering: Rich when it is installed, aligned plain text otherwise."""

from __future__ import annotations

import sys
import threading
from unittest.mock import Mock

import pytest

from lighthouse_cli import display


@pytest.fixture
def unchecked_rich(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(display, "_RICH_CACHE", None)
    monkeypatch.setattr(display, "_RICH_CHECKED", False)


def test_tables_drawn_from_worker_threads_all_use_rich(monkeypatch, unchecked_rich) -> None:
    """All-courses views draw tables from worker threads; none may see Rich as missing.

    A thread that asked while another was still setting Rich up used to get
    no Rich at all and fall back to plain text.
    """
    rich_console = pytest.importorskip("rich.console")
    real_console = rich_console.Console
    setting_up, release = threading.Event(), threading.Event()

    def slow_console(*args, **kwargs):
        setting_up.set()
        release.wait(5)
        return real_console(*args, **kwargs)

    monkeypatch.setattr(rich_console, "Console", slow_console)
    first: list[object] = []
    worker = threading.Thread(target=lambda: first.append(display._try_rich()))
    worker.start()
    try:
        assert setting_up.wait(5)
        # Rich is not marked checked until it is set up, so a second caller waits for it.
        assert not display._RICH_CHECKED
        threading.Timer(0.2, release.set).start()
        second = display._try_rich()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert second is not None
    assert first[0] is second  # one Console, set up once


def test_a_broken_rich_fails_once_then_tables_are_plain_text(monkeypatch, unchecked_rich) -> None:
    rich_console = pytest.importorskip("rich.console")
    monkeypatch.setattr(rich_console, "Console", Mock(side_effect=RuntimeError("broken install")))

    with pytest.raises(RuntimeError):
        display._try_rich()
    assert display._try_rich() is None


def test_without_rich_tables_are_aligned_plain_text(monkeypatch, unchecked_rich, capsys) -> None:
    monkeypatch.setitem(sys.modules, "rich.table", None)  # makes the import raise ImportError

    display.print_table(["ID", "Name"], [["1", "Alpha"], ["22", "B"]], title="Courses")

    assert capsys.readouterr().out == "\nCourses\nID  Name \n--  -----\n1   Alpha\n22  B    \n"
