"""Extract Brightspace's same-session CSRF bootstrap without executing scripts."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from threading import Lock

_TOKEN = re.compile(
    r"localStorage\s*\.\s*setItem\(\s*(['\"])XSRF\.Token\1\s*,\s*(['\"])([A-Za-z0-9._~+/=\-]{1,4096})\2\s*\)"
)


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_script = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False

    def handle_data(self, data: str) -> None:
        if self.in_script:
            self.parts.append(data)


def csrf_from_homepage(body: bytes) -> str:
    parser = _Scripts()
    parser.feed(body.decode("utf-8", errors="replace"))
    matches = {match.group(3) for script in parser.parts for match in _TOKEN.finditer(script)}
    if len(matches) != 1:
        raise ValueError("Could not initialize request protection.")
    return matches.pop()


@dataclass(repr=False)
class FormProtection:
    """Session-bound form protection; neither value belongs in diagnostics."""

    csrf_token: str = field(repr=False)
    hit_code_seed: str = field(repr=False)
    _last_tick: int = field(default=-1, repr=False)
    _counter: int = field(default=0, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def next_hit_code(self) -> str:
        # Mirrors UI.GenerateHitCode in D2L.LP.Web.Core.js. Advance a logical
        # millisecond if more than ten actions occur within one clock tick.
        with self._lock:
            tick = max(time.time_ns() // 1_000_000, self._last_tick)
            self._counter = (self._counter + 1) % 10
            if tick == self._last_tick and self._counter == 0:
                tick += 1
            self._last_tick = tick
            return self.hit_code_seed + str(tick % 100_000_000) + str(self._counter)


_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_XSRF_INIT = "D2L.LP.Web.Authentication.Xsrf.Init"


def form_protection_from_homepage(body: bytes) -> FormProtection:
    """Read the declarative XSRF initializer as data; never eval page scripts.

    Brightspace's object graph embeds a JSON record inside a JSON string:
    {"_type":"func","N":initializer,"P":[parameter,token,numeric_seed]}.
    Only that exact record is recognized. Other function records are ignored.
    """
    if not isinstance(body, bytes) or len(body) > 2 * 1024 * 1024:
        raise ValueError("Could not initialize form protection.")
    parser = _Scripts()
    parser.feed(body.decode("utf-8", errors="replace"))
    found: set[tuple[str, str]] = set()
    for script in parser.parts:
        for match in _JSON_STRING.finditer(script):
            try:
                literal = json.loads(match.group(0))
                if _XSRF_INIT not in literal or not literal.startswith("{"):
                    continue
                record = json.loads(literal)
            except (ValueError, RecursionError):
                continue
            if not isinstance(record, dict) or record.get("_type") != "func" or record.get("N") != _XSRF_INIT:
                continue
            args = record.get("P")
            if not isinstance(args, list) or len(args) != 3 or args[0] != "d2l_referrer":
                continue
            token, seed = args[1:]
            if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/=\-]{1,4096}", token)
                    or type(seed) is not int or not 0 <= seed < 10**16):
                continue
            found.add((token, str(seed)))
    if len(found) != 1:
        raise ValueError("Could not initialize form protection.")
    token, seed = found.pop()
    return FormProtection(token, seed)
