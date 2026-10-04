"""Tests for multi-course JSON output format and synced_at timestamp."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

from lighthouse_cli.api import LighthouseClient
from lighthouse_cli.cli import cli
from lighthouse_cli.manifest import compute_sha256

SEM_I = {"OrgUnitId": 100, "Name": "Sem I", "Code": "S1"}
SEM_II = {"OrgUnitId": 200, "Name": "Sem II", "Code": "S2"}
COURSE_A = (111, "Course A", "S1", "Sem I")
COURSE_B = (222, "Course B", "S1", "Sem I")
ISO_UTC = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"


def _toc_with(*titles):
    """get_content_toc stub: one "Mod" module whose topics get TopicId cid * 10 + index."""
    def get_content_toc(cid):
        return {"Modules": [{"ModuleId": cid, "Title": "Mod", "Modules": [], "Topics": [
            {"TopicId": cid * 10 + i, "Title": title, "TypeIdentifier": "File",
             "Url": "", "LastModifiedDate": "2026-01-01T00:00:00Z"}
            for i, title in enumerate(titles)
        ]}]}
    return get_content_toc


ONE_FILE_TOC = _toc_with("f.pdf")


def _no_modules(cid):
    return {"Modules": []}


def _download(cid, tid):
    return f"content{tid}".encode(), "f.pdf"


def _run_json(cli_runner, tmp_path, args, courses, semesters=(SEM_I,), toc=ONE_FILE_TOC,
              download=_download, exit_code=0):
    """Run ``lighthouse ARGS -o tmp_path/out --json`` against a patched client and parse stdout.

    ``courses`` holds (OrgUnitId, Name, Code, semester label) rows served as the
    enrollments and catalog, and tracked in course-config.json.
    """
    output_dir = tmp_path / "out"
    output_dir.mkdir(exist_ok=True)
    cfg_path = tmp_path / "course-config.json"
    cfg_path.write_text(json.dumps({"tracked_courses": {
        str(oid): {"name": name, "semester": label} for oid, name, _code, label in courses
    }}))
    enrollments = [
        {"OrgUnit": {"Id": oid, "Name": name, "Code": code}} for oid, name, code, _ in courses
    ]
    catalog = [{"OrgUnitId": oid, "Name": name, "Code": code} for oid, name, code, _ in courses]
    with patch("lighthouse_cli.course_config.COURSE_CONFIG_FILE", cfg_path), \
         patch.object(LighthouseClient, "get_semesters", return_value=list(semesters)), \
         patch.object(LighthouseClient, "get_course_enrollments", return_value=enrollments), \
         patch.object(LighthouseClient, "get_courses", return_value=catalog), \
         patch.object(LighthouseClient, "get_content_toc", side_effect=toc), \
         patch.object(LighthouseClient, "download_topic_file", side_effect=download), \
         patch.object(LighthouseClient, "get_topic_html", return_value=(b"", "empty.html")):
        result = cli_runner.invoke(cli, [*args, "-o", str(output_dir), "--json"])
    assert result.exit_code == exit_code, f"exit={result.exit_code} output={result.output}"
    return json.loads(result.output)


def _assert_envelope(data, semester):
    assert {"semester", "synced_at", "summary", "courses"} <= data.keys()
    assert data["semester"] == semester
    assert isinstance(data["courses"], list)
    ts = data["synced_at"]
    assert re.match(ISO_UTC, ts), f"synced_at '{ts}' is not valid UTC ISO 8601"


class TestMultiCourseJsonOutput:
    """Tests for structured JSON output in download/sync multi-course operations."""

    def test_download_multi_course_json_includes_semester_synced_at_summary(self, cli_runner, tmp_path):
        data = _run_json(cli_runner, tmp_path, ["download", "--semester", "100"], [COURSE_A, COURSE_B])

        _assert_envelope(data, {"id": 100, "name": "Sem I"})
        assert len(data["courses"]) == 2

    def test_download_multi_course_summary_counts_consistent(self, cli_runner, tmp_path):
        data = _run_json(
            cli_runner, tmp_path, ["download", "--semester", "100"], [COURSE_A],
            toc=_toc_with("a.pdf", "b.pdf"),
        )

        summary = data["summary"]
        courses = data["courses"]
        assert summary["courses_checked"] == len(courses)
        assert summary["downloaded"] == sum(len(c["downloaded"]) for c in courses)
        assert summary["errors"] == sum(len(c["errors"]) for c in courses)
        for c in courses:
            assert {"downloaded", "skipped", "updated", "duplicates", "errors"} <= c.keys()

    def test_sync_multi_course_json_includes_synced_at_and_summary(self, cli_runner, tmp_path):
        data = _run_json(cli_runner, tmp_path, ["sync", "--semester", "100"], [COURSE_A])

        _assert_envelope(data, {"id": 100, "name": "Sem I"})

    def test_download_multi_course_per_course_has_sha256_extension_size_kb(self, cli_runner, tmp_path):
        def download(cid, tid):
            # Use enough content so size_kb > 0 (at least 1024 bytes = 1 KB)
            return b"X" * 2048, "Lecture.pdf"

        data = _run_json(
            cli_runner, tmp_path, ["download", "--semester", "100"], [COURSE_A],
            toc=_toc_with("Lecture.pdf"), download=download,
        )

        course = data["courses"][0]
        assert len(course["downloaded"]) == 1
        entry = course["downloaded"][0]
        assert len(entry["sha256"]) == 64  # SHA-256 hex length
        assert entry["extension"] == ".pdf"
        assert entry["size_kb"] > 0

    def test_download_multi_course_sha256_dedup_per_course(self, cli_runner, tmp_path):
        """SHA-256 dedup detects same file in different topics within a course."""
        def download(cid, tid):
            return b"IDENTICAL FILE CONTENT", "file.pdf"

        data = _run_json(
            cli_runner, tmp_path, ["download", "--semester", "100"], [COURSE_A],
            toc=_toc_with("Assignment.pdf", "Assignment-Dup.pdf"), download=download,
        )

        course = data["courses"][0]
        assert len(course["downloaded"]) == 2
        hashes = [e["sha256"] for e in course["downloaded"]]
        assert hashes[0] == hashes[1], "Same content should produce same SHA-256"
        assert len(course["duplicates"]) == 2  # both entries are duplicates
        for dup in course["duplicates"]:
            assert {"topic_id", "filename", "sha256"} <= dup.keys()
            assert dup["sha256"] == hashes[0]

    def test_download_multi_course_json_empty_course_exit_0(self, cli_runner, tmp_path):
        data = _run_json(
            cli_runner, tmp_path, ["download", "--semester", "100"], [COURSE_A], toc=_no_modules,
        )

        assert data["summary"]["downloaded"] == 0
        assert data["summary"]["errors"] == 0

    def test_download_multi_course_error_on_one_course_exit_1(self, cli_runner, tmp_path):
        """A partial failure (one course fails) exits 1."""
        def download(cid, tid):
            if cid == 222:
                raise Exception("Network error for course B")
            return _download(cid, tid)

        data = _run_json(
            cli_runner, tmp_path, ["download", "--semester", "100"], [COURSE_A, COURSE_B],
            download=download, exit_code=1,
        )

        course_a = next(c for c in data["courses"] if c["course_id"] == 111)
        course_b = next(c for c in data["courses"] if c["course_id"] == 222)
        assert len(course_a["downloaded"]) == 1
        assert len(course_a["errors"]) == 0
        assert len(course_b["downloaded"]) == 0
        assert len(course_b["errors"]) == 1
        assert "Network error" in course_b["errors"][0]["error"]

    def test_download_multi_course_also_errors_in_json_output(self, cli_runner, tmp_path):
        """An invalid --also is a partial success: exit 0 with also_errors in the JSON."""
        data = _run_json(
            cli_runner, tmp_path, ["download", "--semester", "100", "--also", "99999"], [COURSE_A],
        )

        assert data["also_errors"] == ["Course not found. Run: lighthouse courses"]

    def test_download_all_courses_json_includes_semester_and_summary(self, cli_runner, tmp_path):
        """Download without course_id covers only the latest (highest OrgUnitId) semester."""
        data = _run_json(
            cli_runner, tmp_path, ["download"], [COURSE_A, (222, "Course B", "S2", "Sem II")],
            semesters=(SEM_I, SEM_II),
        )

        _assert_envelope(data, {"id": 200, "name": "Sem II"})
        assert len(data["courses"]) == 1
        assert data["courses"][0]["course_id"] == 222

    def test_sync_all_courses_json_includes_semester_and_summary(self, cli_runner, tmp_path):
        data = _run_json(
            cli_runner, tmp_path, ["sync"], [COURSE_A, (222, "Course B", "S2", "Sem II")],
            semesters=(SEM_I, SEM_II),
        )

        _assert_envelope(data, {"id": 200, "name": "Sem II"})
        assert len(data["courses"]) == 1

    def test_sync_multi_course_skipped_and_updated_in_per_course(self, cli_runner, tmp_path):
        """Sync multi-course JSON includes skipped and updated entries per course."""
        # Pre-seed the manifest (in _run_json's output dir) with one file that
        # hasn't changed (skipped) and one with an older last_modified (updated).
        course_dir = tmp_path / "out" / "Course A-111"
        (course_dir / "Mod").mkdir(parents=True)
        unchanged_content = b"x" * 100
        old_updated_content = b"old updated content"
        (course_dir / ".lighthouse.json").write_text(json.dumps({
            "1110": {
                "sha256": compute_sha256(unchanged_content),
                "filename": "f.pdf",
                "size": len(unchanged_content),
                "downloaded_at": "2026-01-01T00:00:00Z",
                "last_modified": "2026-01-01T00:00:00Z",
            },
            "1111": {
                "sha256": compute_sha256(old_updated_content),
                "filename": "g.pdf",
                "size": len(old_updated_content),
                "downloaded_at": "2025-12-01T00:00:00Z",
                "last_modified": "2025-12-01T00:00:00Z",
            },
        }))
        (course_dir / "Mod" / "f.pdf").write_bytes(unchanged_content)

        def get_content_toc(cid):
            return {"Modules": [{"ModuleId": cid, "Title": "Mod", "Modules": [], "Topics": [
                # Same timestamp → skipped
                {"TopicId": 1110, "Title": "f.pdf", "TypeIdentifier": "File", "Url": "",
                 "LastModifiedDate": "2026-01-01T00:00:00Z"},
                # Different (older) timestamp in manifest → newer in TOC → updated
                {"TopicId": 1111, "Title": "g.pdf", "TypeIdentifier": "File", "Url": "",
                 "LastModifiedDate": "2026-02-01T00:00:00Z"},
            ]}]}

        def download_file(cid, tid):
            return f"content{tid}".encode(), f"file{tid}.pdf"

        data = _run_json(
            cli_runner, tmp_path, ["sync", "--semester", "100"], [COURSE_A],
            toc=get_content_toc, download=download_file,
        )

        course = data["courses"][0]
        assert len(course["errors"]) == 0, f"Unexpected errors: {course['errors']}"
        assert len(course["skipped"]) == 1, f"Expected 1 skipped, got {course['skipped']}"
        assert course["skipped"][0]["topic_id"] == "1110"
        assert course["skipped"][0]["sha256"] == compute_sha256(unchanged_content)
        assert len(course["updated"]) == 1, f"Expected 1 updated, got {course['updated']}"
