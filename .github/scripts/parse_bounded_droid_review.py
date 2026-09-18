#!/usr/bin/env python3
"""Validate a bounded Droid stream-json review and prepare a safe PR comment."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA = "lighthouse.droid-review.v1"
SEVERITIES = {"P0", "P1", "P2", "P3"}
MAX_FINDINGS = 50
MAX_TEXT = 4000


def fail(message: str) -> None:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def redact(text: str) -> str:
    pattern = (
        r"(?i)(d2lSecureSessionVal|d2lSessionVal|d2lSameSiteCanary[AB]|SAMLResponse|"
        r"CLIPROXY_CLAUDE_CODE_API_KEY|DROID_BYOK_ZAI_CODING_API_KEY|ZAI_CODING_API_KEY)"
        r"\s*[:=]\s*[\"']?[^\s,;}\"']+"
    )
    return re.sub(pattern, r"\1=[REDACTED]", text)


def read_events(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    model: str | None = None
    result: dict[str, Any] | None = None
    for raw_line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            candidate = event.get("model")
            if isinstance(candidate, str):
                model = candidate
        if event.get("type") == "result":
            result = event
    return model, result


def required_text(value: Any, field: str, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(f"Droid verdict field {field!r} is missing or empty")
    if len(value) > limit:
        fail(f"Droid verdict field {field!r} is too large")
    return value.strip()


def main() -> None:
    result_file = Path(os.environ["RESULT_FILE"])
    verdict_file = Path(os.environ["VERDICT_FILE"])
    comment_file = Path(os.environ["COMMENT_FILE"])
    expected_model = os.environ["EXPECTED_MODEL"]
    expected_reasoning = os.environ["EXPECTED_REASONING"]
    base_sha = os.environ["BASE_SHA"]
    head_sha = os.environ["HEAD_SHA"]
    if not result_file.is_file():
        fail("Droid result stream is missing")

    model, result_event = read_events(result_file)
    if model != expected_model:
        fail("Droid did not initialize with the requested custom model")
    if result_event is None:
        fail("Droid result event is missing")
    if result_event.get("is_error") is True:
        fail("Droid returned a model error")
    raw_result = result_event.get("result")
    if not isinstance(raw_result, str) or not raw_result.strip():
        fail("Droid result payload is missing")
    try:
        verdict = json.loads(raw_result)
    except json.JSONDecodeError as exc:
        fail(f"Droid returned non-JSON verdict: {exc.msg}")
    if not isinstance(verdict, dict):
        fail("Droid verdict is not a JSON object")

    if verdict.get("schema") != SCHEMA:
        fail("Droid verdict schema is missing or unsupported")
    if verdict.get("model") != expected_model:
        fail("Droid verdict model does not match the initialized model")
    if verdict.get("reasoning_effort") != expected_reasoning:
        fail("Droid verdict reasoning effort does not match the requested tier")
    if verdict.get("base_sha") != base_sha or verdict.get("head_sha") != head_sha:
        fail("Droid verdict does not identify the exact reviewed base/head")
    summary = required_text(verdict.get("summary"), "summary", limit=1000)
    stated_verdict = verdict.get("verdict")
    if stated_verdict not in {"pass", "block"}:
        fail("Droid verdict must be pass or block")
    findings = verdict.get("findings")
    if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
        fail("Droid findings must be a bounded JSON list")

    safe_findings: list[dict[str, Any]] = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            fail(f"Droid finding {index} is not an object")
        severity = finding.get("severity")
        if severity not in SEVERITIES:
            fail(f"Droid finding {index} has an invalid severity")
        if not isinstance(finding.get("high_value"), bool):
            fail(f"Droid finding {index} has no boolean high_value field")
        path = required_text(finding.get("path"), f"findings[{index}].path", limit=512)
        title = required_text(finding.get("title"), f"findings[{index}].title", limit=1000)
        body = required_text(finding.get("body"), f"findings[{index}].body")
        line = finding.get("line")
        if line is not None and (not isinstance(line, int) or line < 1):
            fail(f"Droid finding {index} has an invalid line")
        safe_findings.append(
            {
                "severity": severity,
                "high_value": finding["high_value"],
                "path": redact(path),
                "line": line,
                "title": redact(title),
                "body": redact(body),
            }
        )

    blocking = [finding for finding in safe_findings if finding["severity"] in {"P0", "P1"}]
    expected_verdict = "block" if blocking else "pass"
    if stated_verdict != expected_verdict:
        fail("Droid verdict does not match its P0/P1 findings")

    lines = [
        "<!-- lighthouse-bounded-droid-review -->",
        "## Bounded Droid review",
        "",
        f"- Verdict: **{stated_verdict.upper()}**",
        f"- Model: `{expected_model}` (reasoning: `{expected_reasoning}`)",
        f"- Reviewed base: `{base_sha}`",
        f"- Reviewed head: `{head_sha}`",
        "- Tool scope: `Read`, `Grep`, `Glob`, `LS`",
        "",
        summary,
    ]
    if safe_findings:
        lines.extend(("", "### Findings"))
        for finding in safe_findings:
            location = f"`{finding['path']}`"
            if finding["line"] is not None:
                location += f":{finding['line']}"
            lines.extend(
                (
                    "",
                    f"- **{finding['severity']}** {location} — {finding['title']}",
                    f"  {finding['body']}",
                )
            )
    else:
        lines.extend(("", "No actionable findings."))

    comment_file.write_text(json.dumps({"body": "\n".join(lines)}, ensure_ascii=False))
    verdict_file.write_text(
        json.dumps(
            {
                "verdict": stated_verdict,
                "blocking_findings": len(blocking),
                "base_sha": base_sha,
                "head_sha": head_sha,
                "model": expected_model,
                "reasoning_effort": expected_reasoning,
            }
        )
    )
    print(
        f"Validated bounded Droid verdict: {stated_verdict}; "
        f"blocking_findings={len(blocking)}; model={expected_model}; head={head_sha}"
    )


if __name__ == "__main__":
    main()
