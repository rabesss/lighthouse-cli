"""Assessment operations over the existing bounded, non-replaying transport.

Sources: D2L developer reference, res/quiz.html and res/dropbox.html.
Quiz authoring/inspection APIs do not implement learner attempts.
"""

from __future__ import annotations

import math
from typing import Any

import requests

from .api import (
    LighthouseClient,
    NetworkError,
    SessionExpiredError,
    _close_response,
    _require_positive_endpoint_id,
)
from .display import safe_display_text


class AssessmentWriteUnknownError(NetworkError):
    """The server may have accepted a write; callers must inspect before retrying."""


def positive_id(value: object) -> int:
    return _require_positive_endpoint_id(value, "identifier")


def rich_text(text: str) -> dict[str, str]:
    return {"Content": text, "Type": "Text"}


def quiz_payload(name: str, layout: str, attempts: int) -> dict[str, Any]:
    if not name.strip() or len(name) > 256 or layout not in {"all", "one-way"}:
        raise ValueError("Invalid quiz settings.")
    if type(attempts) is not int or not 1 <= attempts <= 10:
        raise ValueError("Attempts must be between 1 and 10.")
    text = {"Text": rich_text(""), "IsDisplayed": False}
    return {
        "Name": name,
        "IsActive": False,
        "SortOrder": 0,
        "AutoExportToGrades": False,
        "GradeItemId": None,
        "IsAutoSetGraded": False,
        "Instructions": text,
        "Description": text,
        "StartDate": None,
        "EndDate": None,
        "DueDate": None,
        "DisplayInCalendar": False,
        "NumberOfAttemptsAllowed": attempts,
        "LateSubmissionInfo": {"LateSubmissionOption": 0, "LateLimitMinutes": None},
        # These creation defaults were accepted by LE 1.93 in the trial.
        # The duration is dormant because IsEnforced remains false.
        "SubmissionTimeLimit": {"IsEnforced": False, "ShowClock": False, "TimeLimitValue": 120},
        "SubmissionGracePeriod": 0,
        "Password": None,
        "Header": text,
        "Footer": text,
        "AllowHints": False,
        "DisableRightClick": False,
        "DisablePagerAndAlerts": False,
        "NotificationEmail": None,
        "CalcTypeId": 1,
        "RestrictIPAddressRange": None,
        "CategoryId": None,
        "PreventMovingBackwards": layout == "one-way",
        "Shuffle": False,
        "AllowOnlyUsersWithSpecialAccess": False,
        "IsRetakeIncorrectOnly": False,
        "PagingTypeId": 1 if layout == "one-way" else 0,
        "IsSynchronous": False,
        "DeductionPercentage": None,
        "HideQuestionPoints": False,
        "IsSingleSession": False,
    }


def assignment_payload(name: str, instructions: str, submission_type: str) -> dict[str, Any]:
    if not name.strip() or len(name) > 256 or len(instructions) > 65536:
        raise ValueError("Invalid assignment settings.")
    if submission_type not in {"file", "text"}:
        raise ValueError("Invalid submission type.")
    return {
        "Name": name,
        "CategoryId": None,
        "CustomInstructions": rich_text(instructions),
        "Availability": None,
        "GroupTypeId": None,
        "DueDate": None,
        "DisplayInCalendar": False,
        "NotificationEmail": None,
        "IsHidden": True,
        "Assessment": None,
        "IsAnonymous": False,
        "DropboxType": 2,
        "SubmissionType": 0 if submission_type == "file" else 1,
        "CompletionType": 0,
        "GradeItemId": None,
        "AllowOnlyUsersWithSpecialAccess": False,
    }


# Unknown fields, URLs and authentication material never enter CLI output.
# Keep the projection shared across teacher and learner responses; permission
# enforcement belongs to Brightspace, not a caller-selected role flag.
_FIELDS = frozenset(
    """
Id QuizId QuestionId QuestionTypeId Name Title Points Difficulty Bonus Mandatory
QuestionText QuestionInfo SectionId QuestionTemplateId QuestionTemplateVersionId
Text Html Content Type Answers Answer TextAnswer Weight IsCorrect Options
AttemptId UserId AttemptNumber Score Started Completed AttemptFeedback
IsPublished FeedbackLastModified IsRetakeIncorrectOnly AttemptDueDate
AttemptEnforceTimeLimit AttemptSubmissionTimeLimit AttemptIsSynchronous
Entity Status Feedback Submissions SubmittedBy DisplayName SubmissionDate Comment
Files FileId FileName Size isRead isFlagged CompletionDate IsGraded GradedSymbol
Identifier RoleId ClasslistRoleDisplayName LastAccessed IsOnline EntityId EntityType
CategoryId CustomInstructions Attachments TotalFiles UnreadFiles TotalUsers
TotalUsersWithSubmissions TotalUsersWithFeedback Availability StartDate EndDate
DueDate IsHidden Assessment ScoreDenominator DropboxType SubmissionType
CompletionType GradeItemId AllowOnlyUsersWithSpecialAccess IsAnonymous
IsActive PagingTypeId PreventMovingBackwards AttemptsAllowed IsUnlimited
NumberOfAttemptsAllowed SubmissionTimeLimit IsEnforced TimeLimitValue
Instructions Description IsDisplayed AutoExportToGrades Shuffle IsSingleSession
RubricAssessments RubricId OverallScore OverallLevel Criteria CriterionId LevelId
SurveyId ChecklistId ChecklistItemId ForumId TopicId PostId ParentPostId
Subject Message DatePosted LastEdited CreatedDate LastModifiedDate
HasInstantFeedback UserResponses Submission HasDueDate DueDate IsComplete
IsLocked AllowAnonymous RequiresApproval MustPostToParticipate IsDeleted
IsPinned IsApproved IsRead IsFlagged ReplyCount UnreadPostCount TotalPostCount
PostStartDate PostEndDate StartDateAvailabilityType EndDateAvailabilityType
SortOrder CompletionType DueDateDisplay HasExpiryDate ExpiryDate
WordCount AttachmentCount PostingUserDisplayName PostingUserId ThreadId
LastEditDate LastEditedBy CanRate ReplyPostIds UnlockStartDate UnlockEndDate
AllowAnonymousPosts UnapprovedPostCount PinnedPostCount ScoringType IsAutoScore
ScoreOutOf IncludeNonScoredValues ScoredCount RatingsSum RatingsCount
AnswerFeedback AnswerKey AnswerText Blanks Boxes Columns EnableAttachments
EnableStudentEditor Enumeration EvaluationType FalseFeedback FalsePartId
FalseWeight GradingType Hint InitialText NaOption PartId PartIds Randomize
Rows Scale Statement Statements Style Texts TrueFeedback TruePartId TrueWeight
GroupId GroupCategoryId Code Enrollments Groups EnrollmentStyle EnrollmentQuantity
MaxUsersPerGroup AutoEnroll RandomizeEnrollments AllocateAfterExpiry
SelfEnrollmentExpiryDate DescriptionsVisibleToEnrolees
""".split()
)


def project(value: Any) -> Any:
    """Bounded recursive allowlist, with no raw objects or exception strings."""
    remaining = 20000

    def walk(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError("Assessment response exceeds output limits.")
        if item is None or type(item) is bool:
            return item
        if type(item) in {int, float}:
            return item if abs(item) < 1e18 and math.isfinite(item) else None
        if isinstance(item, str):
            return safe_display_text(item, "", max_len=65536)
        if isinstance(item, list):
            return [walk(child, depth + 1) for child in item]
        if isinstance(item, dict):
            return {key: walk(child, depth + 1) for key, child in item.items() if key in _FIELDS}
        return None

    return walk(value, 0)


class AssessmentAPI:
    def __init__(self, client: LighthouseClient, course_id: int) -> None:
        self.client = client
        self.course_id = positive_id(course_id)

    def path(self, resource: str, identifier: int | None = None) -> str:
        root = {"quiz": "quizzes", "assignment": "dropbox/folders"}[resource]
        suffix = f"/{positive_id(identifier)}" if identifier is not None else "/"
        return f"/{self.course_id}/{root}{suffix}"

    def read(self, resource: str, identifier: int | None = None) -> Any:
        path = self.path(resource, identifier)
        if identifier is None:
            return self.client._paginate_list(path)
        return self.client.get_json(path)

    def questions(self, quiz_id: int) -> list[dict[str, Any]]:
        return self.client._paginate_list(self.path("quiz", quiz_id) + "/questions/")

    def attempts(self, quiz_id: int) -> list[dict[str, Any]]:
        return self.client._paginate_list(self.path("quiz", quiz_id) + "/attempts/")

    def submissions(self, folder_id: int, *, mine: bool) -> Any:
        path = self.path("assignment", folder_id) + "/submissions/"
        if mine:
            return self.client.get_json(path + "mysubmissions/")
        return self.client.get_json(path)

    def write(
        self, method: str, resource: str, data: dict[str, Any], identifier: int | None = None
    ) -> Any:
        if method not in {"POST", "PUT"}:
            raise ValueError("Unsupported assessment operation.")
        url = self.client.canonical_url(self.path(resource, identifier))
        # Homepage protection is a read-only prerequisite. If it fails, no
        # assessment write was attempted and callers may safely retry it.
        csrf_token = self.client.get_csrf_token()
        try:
            response = self.client._request(
                method, url, json=data, headers={"X-Csrf-Token": csrf_token}
            )
        except (NetworkError, SessionExpiredError):
            raise AssessmentWriteUnknownError(
                "Write outcome unknown. Inspect the assessment before retrying."
            ) from None
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429 or (isinstance(status, int) and status >= 500):
                raise AssessmentWriteUnknownError(
                    "Write outcome unknown. Inspect the assessment before retrying."
                ) from None
            raise
        try:
            if response.status_code == 204:
                if method == "POST":
                    raise ValueError()
                return None
            result = response.json()
            if method == "POST":
                if not isinstance(result, dict):
                    raise ValueError()
                positive_id(result.get("QuizId" if resource == "quiz" else "Id"))
            return result
        except Exception:
            raise AssessmentWriteUnknownError(
                "Write response could not be verified. Inspect the assessment before retrying."
            ) from None
        finally:
            _close_response(response)
