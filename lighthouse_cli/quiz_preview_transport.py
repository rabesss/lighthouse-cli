"""Low-level start, read, save and forward transitions for instructor previews.

The CLI session layer owns encrypted cursors and uncertain-outcome recovery.
Only preview pages are accepted. No student attempt can be written here.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode, urlparse, parse_qs

from bs4 import BeautifulSoup

from .api import LighthouseClient, NetworkError, _close_response
from .quiz_attempt_page import MAX_PAGE_BYTES, PreviewPage, parse_preview_page, hidden_form
from .request_protection import form_protection_from_homepage


class PreviewSaveUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Answer save could not be verified. Inspect the preview before retrying.")


class PreviewStartUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Preview start could not be verified. Inspect quiz attempts before starting again.")


class PreviewAdvanceUnknownError(NetworkError):
    def __init__(self) -> None:
        super().__init__("Preview navigation could not be verified. Inspect the browser before continuing.")


def _start_target(client: LighthouseClient, value: str, filename: str, course_id: int, quiz_id: int) -> str:
    url = client.canonical_url(value)
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed = {"ou", "qi", "isprv", "dnb", "cfql", "fromQB", "inProgress", "cft", "d2l_body_type"}
    if (parsed.path != f"/d2l/lms/quizzing/user/attempt/{filename}"
            or set(query) - allowed
            or any(query.get(key) != [str(value)] for key, value in {"ou": course_id, "qi": quiz_id, "isprv": 1, "fromQB": 0, "inProgress": 0}.items())):
        raise NetworkError("Preview start form has an unexpected identity.")
    return url


def start_preview(client: LighthouseClient, *, course_id: int, quiz_id: int, bypass_availability: bool = False) -> PreviewPage:
    """Start an instructor preview once; availability bypass is explicit."""
    # Reuse strict identity validation before any request.
    page_path(course_id, quiz_id, 1, 1)
    if type(bypass_availability) is not bool:
        raise ValueError("Invalid preview settings.")
    summary = "/d2l/lms/quizzing/user/quiz_summary.d2l?" + urlencode({
        "bp": int(bypass_availability), "isprv": 1, "qi": quiz_id, "ou": course_id,
    })
    body, _ = client.get_raw(summary, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    soup = BeautifulSoup(body, "html.parser")
    if not any(button.get_text(" ", strip=True) == "Start Quiz!" and not button.has_attr("disabled")
               for button in soup.find_all("button")):
        raise NetworkError("Preview start is not available to this account or under the current quiz restrictions.")
    form, fields = hidden_form(body)
    if form.select('input[type="password"]') or fields.get("hps"):
        raise NetworkError("This preview requires an additional browser authorization step.")
    protection = form_protection_from_homepage(body)
    if fields.get("d2l_referrer") != protection.csrf_token:
        raise NetworkError("Preview form protection could not be verified.")
    fields.update(d2l_action="Custom", d2l_actionparam="1", d2l_hitCode=protection.next_hit_code())
    if bypass_availability:
        fields["bypass"] = "1"
    post_url = client.canonical_url(summary + "&cfql=0&inProgress=0")
    response = None
    try:
        # The summary POST registers the preview/bypass choice. Skipping it
        # can appear to work for visible quizzes but fails for hidden ones.
        response = client._request("POST", post_url, _skip_raise=True,
                                   files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(summary)})
        if response.status_code != 302:
            raise PreviewStartUnknownError()
        root_url = _start_target(client, response.headers.get("Location", ""), "quiz_start_frame_auto.d2l", course_id, quiz_id)
        _close_response(response)
        response = None
        root, _ = client.get_raw(root_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
        candidates = [f.get("src") for f in BeautifulSoup(root, "html.parser").find_all("iframe")
                      if isinstance(f.get("src"), str) and urlparse(f["src"]).path.endswith("/quiz_start_iframe_2_auto.d2l")]
        if len(candidates) != 1:
            raise PreviewStartUnknownError()
        frame_path = _start_target(client, candidates[0], "quiz_start_iframe_2_auto.d2l", course_id, quiz_id)
        frame, _ = client.get_raw(frame_path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Referer": root_url})
        frames = BeautifulSoup(frame, "html.parser").select('iframe[name="hiddenFrame"], frame[name="hiddenFrame"]')
        if len(frames) != 1 or not isinstance(frames[0].get("src"), str):
            raise PreviewStartUnknownError()
        process_url = _start_target(client, frames[0]["src"], "quiz_start_process_auto.d2l", course_id, quiz_id)
        # This legacy GET creates server state: it is deliberately not replayed.
        result, _ = client.get_raw(process_url, max_bytes=MAX_PAGE_BYTES, _replay_safe=False,
                                  headers={"Referer": client.canonical_url(frame_path)})
        matches: set[tuple[int, int]] = set()
        for script in BeautifulSoup(result, "html.parser").find_all("script"):
            for match in re.finditer(
                r"^\s*parent\.GoToAttemptQuizAuto\(\s*([0-9]{1,18})\s*,\s*([0-9]{1,6})\s*,\s*0\s*\)\s*;?\s*$",
                script.get_text(), re.MULTILINE,
            ):
                matches.add((int(match[1]), int(match[2])))
        if len(matches) != 1:
            raise PreviewStartUnknownError()
        attempt_id, page = matches.pop()
        return read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    except Exception:
        raise PreviewStartUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def page_path(course_id: int, quiz_id: int, attempt_id: int, page: int) -> str:
    if any(type(value) is not int or not 0 < value < 10**18 for value in (course_id, quiz_id, attempt_id, page)):
        raise ValueError("Invalid preview identity.")
    return "/d2l/lms/quizzing/user/attempt/quiz_attempt_page_auto.d2l?" + urlencode({
        "ou": course_id, "qi": quiz_id, "ai": attempt_id, "pg": page,
        "isprv": 1, "d2l_body_type": 3, "fromQB": 0,
    })


def read_current_preview(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> PreviewPage:
    """Read the caller's current cursor; never infer or advance that cursor."""
    path = page_path(course_id, quiz_id, attempt_id, page)
    body, _ = client.get_raw(path, max_bytes=MAX_PAGE_BYTES, _replay_safe=False, headers={"Cache-Control": "no-cache"})
    return parse_preview_page(body, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)


def save_current_preview_answer(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int,
    page: int, question_id: int, choice_id: int,
) -> PreviewPage:
    """Fetch fresh protection and form state, POST once, verify server state.

    A transport error or ambiguous response must not trigger a second POST.
    A successful HTTP status without saved/selected readback is not success.
    """
    path = page_path(course_id, quiz_id, attempt_id, page)
    homepage, _ = client.get_raw("/d2l/home", max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    protection = form_protection_from_homepage(homepage)
    identity = dict(course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    current = read_current_preview(client, **identity)
    fields = current.answer_fields(question_id, choice_id, protection)
    url = client.canonical_url(
        "/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l?"
        + urlencode({"d2l_body_type": 3, "ou": course_id, "fromQB": 0})
    )
    response = None
    try:
        response = client._request(
            "POST", url,
            files=[(key, (None, value)) for key, value in fields.items()],
            headers={"Referer": client.canonical_url(path)},
        )
        if response.status_code != 200:
            raise PreviewSaveUnknownError()
        verified = read_current_preview(client, **identity)
        if not verified.confirms_answer(question_id, choice_id):
            raise PreviewSaveUnknownError()
        return verified
    except Exception:
        raise PreviewSaveUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)


def advance_current_preview(
    client: LighthouseClient, *, course_id: int, quiz_id: int, attempt_id: int, page: int,
) -> PreviewPage:
    homepage, _ = client.get_raw("/d2l/home", max_bytes=MAX_PAGE_BYTES, _replay_safe=False)
    protection = form_protection_from_homepage(homepage)
    current = read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page)
    fields = current.advance_fields(protection)
    url = client.canonical_url("/d2l/lms/quizzing/user/attempt/quiz_attempt_save_auto.d2l?" + urlencode({
        "cfql": 0, "fromQB": 0, "d2l_body_type": 3, "ou": course_id,
    }))
    response = None
    try:
        response = client._request("POST", url, files=[(key, (None, value)) for key, value in fields.items()],
                                   headers={"Referer": client.canonical_url(page_path(course_id, quiz_id, attempt_id, page))})
        if response.status_code != 200:
            raise PreviewAdvanceUnknownError()
        return read_current_preview(client, course_id=course_id, quiz_id=quiz_id, attempt_id=attempt_id, page=page + 1)
    except Exception:
        raise PreviewAdvanceUnknownError() from None
    finally:
        if response is not None:
            _close_response(response)
