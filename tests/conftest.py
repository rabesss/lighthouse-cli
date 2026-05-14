"""Pytest fixtures for lighthouse-cli tests.

Provides reusable fixtures for:
- CliRunner: Click CLI test runner
- mock_api_client: MagicMock-backed LighthouseClient for API mocking
- temp_download_dir: Temporary directory factory for download testing
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from lighthouse_cli.api import LighthouseClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_runner() -> CliRunner:
    """Return a CliRunner instance for invoking CLI commands."""
    return CliRunner()


@pytest.fixture
def mock_api_client() -> MagicMock:
    """Return a MagicMock that quacks like a LighthouseClient.

    Provides safe defaults for common API methods:
    - get_semesters() -> []
    - get_courses() -> []
    - get_content_toc(org_id) -> {"Modules": []}
    - get_announcements(org_id) -> []
    - get_grade_schema(org_id) -> []
    - get_my_grades(org_id) -> []
    - get_quizzes(org_id) -> []
    - get_calendar(org_id) -> []
    - check_auth() -> True

    Tests can override specific methods as needed:
        mock_api_client.get_semesters.return_value = [...]
    """
    mock = MagicMock(spec=LighthouseClient)
    mock.get_semesters.return_value = []
    mock.get_courses.return_value = []
    mock.get_course_enrollments.return_value = []
    mock.get_content_toc.return_value = {"Modules": []}
    mock.get_announcements.return_value = []
    mock.get_grade_schema.return_value = []
    mock.get_my_grades.return_value = []
    mock.get_quizzes.return_value = []
    mock.get_calendar.return_value = []
    mock.check_auth.return_value = True
    return mock


@pytest.fixture
def temp_download_dir(tmp_path: Path) -> Path:
    """Return a temporary directory Path for download testing.

    The directory is automatically created and cleaned up after the test.
    """
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    return download_dir


@pytest.fixture
def sample_semesters() -> list[dict[str, Any]]:
    """Return a sample list of semesters for mocking."""
    return [
        {"OrgUnitId": 58272, "Name": "AY 2025-2026 | Sem IV", "Code": "0902_IV_2025-2026"},
        {"OrgUnitId": 58271, "Name": "AY 2025-2026 | Sem III", "Code": "0902_III_2025-2026"},
        {"OrgUnitId": 44313, "Name": "AY 2024-2025 | Sem II", "Code": "0902_II_2024-2025"},
        {"OrgUnitId": 29337, "Name": "AY 2023-2024 | Sem I", "Code": "0902_I_2023-2024"},
    ]


@pytest.fixture
def sample_courses() -> list[dict[str, Any]]:
    """Return a sample course enrollments for mocking get_course_enrollments.

    Format: list of enrollment dicts as returned by D2L enrollments API.
    """
    return [
        {
            "OrgUnit": {
                "Id": 44347,
                "Name": "Signals & Systems",
                "Code": "009_BME 2125_2025-2026",
            },
            "Access": {"IsActive": True},
        },
        {
            "OrgUnit": {
                "Id": 44348,
                "Name": "Engineering Mathematics III",
                "Code": "009_MAT 2223_2025-2026",
            },
            "Access": {"IsActive": True},
        },
        {
            "OrgUnit": {
                "Id": 44349,
                "Name": "Anatomy & Physiology",
                "Code": "009_BIO 2101_2025-2026",
            },
            "Access": {"IsActive": True},
        },
    ]


@pytest.fixture
def sample_content_toc() -> dict[str, Any]:
    """Return a sample content TOC for mocking."""
    return {
        "Modules": [
            {
                "ModuleId": 1001,
                "Title": "Unit 1 - Introduction to Signals",
                "Modules": [],
                "Topics": [
                    {
                        "TopicId": 12345,
                        "Title": "L1-L2 Introduction to computing.pdf",
                        "TypeIdentifier": "File",
                        "Url": "https://example.com/files/12345",
                    },
                    {
                        "TopicId": 12346,
                        "Title": "L3 Signal Classification.pdf",
                        "TypeIdentifier": "File",
                        "Url": "https://example.com/files/12346",
                    },
                ],
            },
            {
                "ModuleId": 1002,
                "Title": "Unit 2 - Systems",
                "Modules": [],
                "Topics": [
                    {
                        "TopicId": 12347,
                        "Title": "L4 LTI Systems.pdf",
                        "TypeIdentifier": "File",
                        "Url": "https://example.com/files/12347",
                    },
                ],
            },
        ]
    }


@pytest.fixture
def sample_grades_schema() -> list[dict[str, Any]]:
    """Return a sample grade schema for mocking."""
    return [
        {"Id": 1, "Name": "CAT 1", "Weight": "15%", "GradeType": "Points", "MaxPoints": 20},
        {"Id": 2, "Name": "Assignment 1", "Weight": "10%", "GradeType": "Points", "MaxPoints": 10},
        {"Id": 3, "Name": "Midterm", "Weight": "25%", "GradeType": "Points", "MaxPoints": 50},
    ]


@pytest.fixture
def sample_grades_values() -> list[dict[str, Any]]:
    """Return a sample grade values for mocking."""
    return [
        {"GradeObjectIdentifier": "1", "PointsNumerator": 18, "PointsDenominator": 20},
        {"GradeObjectIdentifier": "2", "PointsNumerator": 9, "PointsDenominator": 10},
        {"GradeObjectIdentifier": "3", "PointsNumerator": 42, "PointsDenominator": 50},
    ]


@pytest.fixture
def sample_quizzes() -> list[dict[str, Any]]:
    """Return a sample quiz list for mocking."""
    return [
        {
            "QuizId": 101,
            "Name": "Quiz 1 - Signal Basics",
            "StartDate": "2025-05-10T10:00:00Z",
            "EndDate": "2025-05-10T10:30:00Z",
            "IsActive": True,
        },
        {
            "QuizId": 102,
            "Name": "Quiz 2 - Fourier Transform",
            "StartDate": "2025-05-17T10:00:00Z",
            "EndDate": "2025-05-17T10:30:00Z",
            "IsActive": True,
        },
    ]


@pytest.fixture
def sample_announcements() -> list[dict[str, Any]]:
    """Return a sample announcement list for mocking."""
    return [
        {
            "Id": 9999,
            "Title": "Midsem schedule update",
            "Body": {"Text": "The midsem examination has been rescheduled to May 20th."},
            "CreatedDate": "2025-05-08T14:30:00Z",
            "Attachments": [],
        }
    ]


@pytest.fixture
def sample_calendar_events() -> list[dict[str, Any]]:
    """Return a sample calendar event list for mocking."""
    return [
        {
            "CalendarEventId": "evt-001",
            "Title": "Midsem Examination",
            "StartDateTime": "2025-05-15T10:00:00Z",
            "EndDateTime": "2025-05-15T12:00:00Z",
            "OrgUnitName": "Signals & Systems",
        },
        {
            "CalendarEventId": "evt-002",
            "Title": "Assignment 3 Deadline",
            "StartDateTime": "2025-05-20T23:59:00Z",
            "EndDateTime": "2025-05-20T23:59:00Z",
            "OrgUnitName": "Signals & Systems",
        },
    ]
