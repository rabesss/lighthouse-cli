"""Table rendering: Rich when it is installed, aligned plain text otherwise."""

from __future__ import annotations

import sys
import threading
import time

import pytest
from rich import console as rich_console

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
    real_console = rich_console.Console
    setting_up = threading.Event()

    def slow_console(*args, **kwargs):
        setting_up.set()
        time.sleep(0.2)
        return real_console(*args, **kwargs)

    monkeypatch.setattr(rich_console, "Console", slow_console)
    first: list[object] = []
    worker = threading.Thread(target=lambda: first.append(display._try_rich()))
    worker.start()
    assert setting_up.wait(5)
    second = display._try_rich()
    worker.join(5)

    assert second is not None
    assert first == [second]


def test_without_rich_tables_are_aligned_plain_text(monkeypatch, unchecked_rich, capsys) -> None:
    monkeypatch.setitem(sys.modules, "rich.table", None)  # makes the import raise ImportError

    display.print_table(["ID", "Name"], [["1", "Alpha"], ["22", "B"]], title="Courses")

    assert capsys.readouterr().out == "\nCourses\nID  Name \n--  -----\n1   Alpha\n22  B    \n"
