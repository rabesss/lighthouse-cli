"""Synthetic UI doubles only: never launch a browser or contact Outlook."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lighthouse_cli import outlook_web as web

playwright_api = pytest.importorskip("playwright.sync_api")
Browser = playwright_api.Browser
BrowserContext = playwright_api.BrowserContext
Locator = playwright_api.Locator
Page = playwright_api.Page
Playwright = playwright_api.Playwright
TimeoutError = playwright_api.TimeoutError


def _locator(*, count: int = 1, visible: bool = True) -> MagicMock:
    node = MagicMock(spec=Locator)
    node.count.return_value = count
    node.is_visible.return_value = visible
    node.first = node
    return node


def _message_list(*states: bool | None) -> MagicMock:
    message_list = _locator()
    options = _locator(count=len(states))
    rows = []
    for index, unread in enumerate(states):
        row = _locator()
        row.inner_text.return_value = f"Sender {index + 1}\nSubject {index + 1}\n10:00"
        row.get_attribute.side_effect = lambda attribute, row_id=f"row-{index}": (
            "false" if attribute == "aria-selected" else row_id if attribute == "id" else None
        )
        row.locator.side_effect = lambda selector, state=unread: _locator(
            count=int((selector == 'button[title="Mark as read"]' and state is True) or (selector == 'button[title="Mark as unread"]' and state is False)),
        )
        rows.append(row)
    options.nth.side_effect = lambda index: rows[index]
    message_list.get_by_role.return_value = options
    return message_list


def _page(message_list: MagicMock | None = None) -> MagicMock:
    page = MagicMock(spec=Page)
    page.url = web.OUTLOOK_URL
    page.is_closed.return_value = False
    page.get_by_text.return_value = _locator(visible=False)
    page.get_by_role.return_value = message_list if message_list is not None else _message_list(False)
    return page


@pytest.fixture
def browser_runtime():
    page = _page()
    context = MagicMock(spec=BrowserContext)
    context.new_page.return_value = page
    browser = MagicMock(spec=Browser)
    browser.new_context.return_value = context
    runtime = MagicMock(spec=Playwright)
    runtime.chromium.launch.return_value = browser
    manager = MagicMock()
    manager.__enter__.return_value = runtime
    with patch("playwright.sync_api.sync_playwright", return_value=manager) as start:
        yield page, context, browser, runtime, start


def test_ephemeral_browser_reads_without_any_mail_action_or_session_export(browser_runtime) -> None:
    page, context, browser, runtime, _start = browser_runtime
    result = web.collect_outlook_rows()

    runtime.chromium.launch.assert_called_once_with(headless=False)
    browser.new_context.assert_called_once_with(locale="en-US", accept_downloads=False)
    runtime.chromium.launch_persistent_context.assert_not_called()
    runtime.chromium.connect_over_cdp.assert_not_called()
    context.cookies.assert_not_called()
    context.storage_state.assert_not_called()
    context.add_cookies.assert_not_called()
    context.grant_permissions.assert_not_called()
    page.evaluate.assert_not_called()
    page.click.assert_not_called()
    page.goto.assert_called_once_with(web.OUTLOOK_URL, wait_until="domcontentloaded", timeout=30000)
    page.get_by_role.assert_called_once_with("listbox", name=web._MESSAGE_LIST)
    context.close.assert_called_once()
    browser.close.assert_called_once()
    assert result["scope"] == "current_view"
    assert result["coverage"] == "rendered_rows_only"
    assert result["complete_mailbox"] is False
    assert result["stable_ids"] is False
    assert result["text_included"] is False
    message_list = page.get_by_role.return_value
    message_list.get_by_role.return_value.nth(0).inner_text.assert_not_called()
    assert result["rows"] == [{
        "position": 1, "rendered_text": "", "unread": False, "text_omitted": True,
    }]


def test_rows_expose_only_rendered_text_and_read_state_without_clicks() -> None:
    message_list = _message_list(True, False, None)
    result = web._read_rows(message_list, limit=2)

    assert [row["unread"] for row in result["rows"]] == [True, False]
    assert result["limit_reached"] is True
    options = message_list.get_by_role.return_value
    assert options.nth.call_count == 4
    for call in options.mock_calls:
        assert "click" not in str(call)
    assert all("id" not in row for row in result["rows"])


def test_unavailable_read_state_fails_closed() -> None:
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(_message_list(None), limit=25)
    assert exc.value.code == "unsupported_layout"


@pytest.mark.parametrize("text", ["flow_token=ROW_SENTINEL", "\x1b[31munsafe", "x" * 2049])
def test_sensitive_or_unbounded_row_text_is_omitted(text: str) -> None:
    message_list = _message_list(False)
    row = message_list.get_by_role.return_value.nth(0)
    row.inner_text.return_value = text
    result = web._read_rows(message_list, limit=25)
    assert result["rows"][0]["rendered_text"] == ""
    assert result["rows"][0]["text_omitted"] is True


def test_unicode_and_multiline_row_text_are_withheld_too() -> None:
    message_list = _message_list(False)
    row = message_list.get_by_role.return_value.nth(0)
    row.inner_text.return_value = "München\n研究 meeting\t10:00"
    result = web._read_rows(message_list, limit=25)
    assert result["rows"][0]["rendered_text"] == ""
    row.inner_text.assert_not_called()


def test_changing_or_ambiguous_rows_fail_closed() -> None:
    message_list = _message_list(False)
    row = message_list.get_by_role.return_value.nth(0)
    identities = iter(["before", "after"])
    row.get_attribute.side_effect = lambda attribute: "false" if attribute == "aria-selected" else next(identities)
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "view_changed"

    row.get_attribute.side_effect = None
    row.get_attribute.side_effect = lambda attribute: "false" if attribute == "aria-selected" else "row"
    row.locator.side_effect = None
    row.locator.return_value = _locator()
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "unsupported_layout"


def test_zero_rows_do_not_claim_an_empty_mailbox() -> None:
    message_list = _message_list()
    message_list.get_by_role.return_value.first.wait_for.side_effect = TimeoutError("UI_SENTINEL")
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "empty_or_loading"
    assert "UI_SENTINEL" not in str(exc.value)


@pytest.mark.parametrize(
    ("code", "category"),
    [("90094", "admin_approval_required"), ("65001", "consent_required"), ("65004", "consent_incomplete"),
     ("50131", "access_blocked"), ("53000", "access_blocked"), ("53001", "access_blocked"),
     ("53002", "access_blocked"), ("53003", "access_blocked"), ("53004", "registration_blocked")],
)
@pytest.mark.parametrize("prefix", ["AADSTS", "Error Code: "])
def test_known_policy_pages_stop_without_retrying_or_completing_sign_in(code: str, category: str, prefix: str) -> None:
    page = _page()
    page.url = "https://login.microsoftonline.com/tenant/login"
    page.get_by_text.side_effect = lambda expression: _locator(visible=bool(expression.search(f"{prefix}{code}: details")))
    with pytest.raises(web.OutlookWebError) as exc:
        web._wait_for_message_list(page, timeout=30)
    assert exc.value.code == category
    page.wait_for_timeout.assert_not_called()
    page.get_by_role.assert_not_called()
    page.click.assert_not_called()


def test_mail_content_cannot_be_mistaken_for_an_auth_policy_page() -> None:
    page = _page()
    page.get_by_text.return_value = _locator(visible=True)
    assert web._wait_for_message_list(page, timeout=30) is page.get_by_role.return_value
    page.get_by_text.assert_not_called()


@pytest.mark.parametrize(("url", "category"), [
    ("https://login.microsoftonline.com/tenant/login", "login_timeout"),
    (web.OUTLOOK_URL, "view_timeout"),
])
def test_login_and_ui_waits_are_bounded(url: str, category: str) -> None:
    page = _page(_locator(count=0))
    page.url = url
    with patch.object(web.time, "monotonic", side_effect=[0, 0, 31]):
        with pytest.raises(web.OutlookWebError) as exc:
            web._wait_for_message_list(page, timeout=30)
    assert exc.value.code == category
    page.wait_for_timeout.assert_called_once_with(250)


def test_closed_browser_is_terminal() -> None:
    page = _page()
    page.is_closed.return_value = True
    with pytest.raises(web.OutlookWebError) as exc:
        web._wait_for_message_list(page, timeout=30)
    assert exc.value.code == "browser_closed"


@pytest.mark.parametrize("search", ["course updates", "private query", "x" * 512])
def test_search_fails_before_browser_without_claiming_fresh_results(browser_runtime, search: str) -> None:
    page, _context, _browser, _runtime, start = browser_runtime
    with pytest.raises(web.OutlookWebError) as exc:
        web.collect_outlook_rows(search=search)
    assert exc.value.code == "search_not_supported"
    start.assert_not_called()
    page.get_by_role.assert_not_called()


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 101}, {"limit": True},
    {"login_timeout": 29}, {"login_timeout": 601}, {"search": ""}, {"search": "x" * 513}, {"search": "bad\nquery"}])
def test_invalid_options_do_not_start_browser(browser_runtime, options) -> None:
    _page_obj, _context, _browser, _runtime, start = browser_runtime
    with pytest.raises(web.OutlookWebError) as exc:
        web.collect_outlook_rows(**options)
    assert exc.value.code == "invalid_options"
    start.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("token=BROWSER_SENTINEL"), KeyboardInterrupt()])
def test_error_or_interrupt_cleans_up_context_without_leaking_details(browser_runtime, failure) -> None:
    page, context, browser, _runtime, _start = browser_runtime
    page.get_by_role.side_effect = failure
    expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else web.OutlookWebError
    with pytest.raises(expected) as exc:
        web.collect_outlook_rows()
    assert "BROWSER_SENTINEL" not in str(exc.value)
    context.close.assert_called_once()
    browser.close.assert_called_once()


def test_browser_launch_failure_is_clean(browser_runtime) -> None:
    _page_obj, _context, browser, runtime, _start = browser_runtime
    runtime.chromium.launch.side_effect = RuntimeError("token=LAUNCH_SENTINEL")
    with pytest.raises(web.OutlookWebError) as exc:
        web.collect_outlook_rows()
    assert exc.value.code == "browser_unavailable"
    assert "LAUNCH_SENTINEL" not in str(exc.value)
    browser.new_context.assert_not_called()


def test_unknown_error_code_cannot_be_used_to_echo_upstream_data() -> None:
    error = web.OutlookWebError("token=UNKNOWN_SENTINEL")
    assert error.code == "browser_error"
    assert "UNKNOWN_SENTINEL" not in str(error)


@pytest.mark.parametrize("text", [
    "Your verification code is 123456",
    "Microsoft account team Your single-use code is 123456",
    "Your security code: 123456",
    "One-time password 123456",
    "Your sign-in code is 123456",
    "Your passcode is 123456",
    "Authentication code 123456",
    "TOTP: 123456",
    "Use this code to sign in: 123456",
    "Your Apple ID Code is 123456",
    "Your ChatGPT code is 123456",
    "Microsoft account Verify your email address 123456",
    "123456 is your authentication code",
    "123 456",
    "123-456",
    "Reset your password: https://example.test/reset?opaque=UNKNOWN_SENTINEL",
])
@pytest.mark.parametrize("json_output", [False, True])
def test_authentication_code_previews_never_reach_human_or_json_output(
    browser_runtime, text: str, json_output: bool, capsys: pytest.CaptureFixture[str],
) -> None:
    from lighthouse_cli.outlook_commands import cmd_outlook_probe

    page, _context, _browser, _runtime, _start = browser_runtime
    message_list = _message_list(False)
    message_list.get_by_role.return_value.nth(0).inner_text.return_value = text
    page.get_by_role.return_value = message_list

    assert cmd_outlook_probe(interactive_login=True, json_output=json_output) == 0
    captured = capsys.readouterr()
    assert "123456" not in captured.out + captured.err
    assert "123 456" not in captured.out + captured.err
    assert "123-456" not in captured.out + captured.err
    assert "UNKNOWN_SENTINEL" not in captured.out + captured.err
    message_list.get_by_role.return_value.nth(0).inner_text.assert_not_called()
    if json_output:
        import json

        row = json.loads(captured.out)["rows"][0]
        assert row["text_omitted"] is True
        assert row["rendered_text"] == ""
    else:
        assert "withheld" in captured.out


def test_disappearing_row_fails_instead_of_returning_empty_success() -> None:
    message_list = _message_list(False)
    message_list.get_by_role.return_value.nth(0).is_visible.return_value = False
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=1)
    assert exc.value.code == "view_changed"


def test_selected_row_is_never_returned() -> None:
    message_list = _message_list(False)
    row = message_list.get_by_role.return_value.nth(0)
    row.get_attribute.side_effect = None
    row.get_attribute.return_value = "true"
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "unsupported_layout"
    row.inner_text.assert_not_called()


def test_row_reordering_between_reads_fails_closed() -> None:
    message_list = _message_list(False, False)
    options = message_list.get_by_role.return_value
    first, second = options.nth(0), options.nth(1)
    options.nth.side_effect = [first, second, second]
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "view_changed"


def test_row_count_change_fails_closed() -> None:
    message_list = _message_list(True)
    message_list.get_by_role.return_value.count.side_effect = [1, 2]
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "view_changed"


@pytest.mark.parametrize("url", [
    "http://outlook.office.com/mail/",
    "https://outlook.office.com.evil.test/mail/",
    "https://user@outlook.office.com/mail/",
    "https://outlook.office.com:444/mail/",
    "https://[broken/mail/",
])
def test_untrusted_origins_are_not_mailbox_or_sign_in_surfaces(url: str) -> None:
    page = _page()
    page.url = url
    with patch.object(web.time, "monotonic", side_effect=[0, 0, 31]):
        with pytest.raises(web.OutlookWebError) as exc:
            web._wait_for_message_list(page, timeout=30)
    assert exc.value.code == "login_timeout"
    page.get_by_role.assert_not_called()
    page.get_by_text.assert_not_called()


@pytest.mark.parametrize("identity", [None, "", "x" * 513])
def test_missing_or_unbounded_row_identity_fails_without_reading_labels(identity: str | None) -> None:
    message_list = _message_list(False)
    row = message_list.get_by_role.return_value.nth(0)
    row.get_attribute.side_effect = lambda attribute: "false" if attribute == "aria-selected" else identity
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "unsupported_layout"
    row.inner_text.assert_not_called()
    row.text_content.assert_not_called()


def test_duplicate_dom_rows_fail_without_canonical_id_claims() -> None:
    message_list = _message_list(False, False)
    for index in range(2):
        row = message_list.get_by_role.return_value.nth(index)
        row.get_attribute.side_effect = lambda attribute: "false" if attribute == "aria-selected" else "duplicate"
    with pytest.raises(web.OutlookWebError) as exc:
        web._read_rows(message_list, limit=25)
    assert exc.value.code == "view_changed"


def test_default_collection_does_not_read_raw_row_text_or_accessible_labels(browser_runtime) -> None:
    page, _context, _browser, _runtime, _start = browser_runtime
    message_list = _message_list(False)
    row = message_list.get_by_role.return_value.nth(0)
    row.inner_text.side_effect = AssertionError("Raw message text must not be read")
    row.text_content.side_effect = AssertionError("Raw message text must not be read")
    def attribute(name: str) -> str:
        assert name in {"aria-selected", "id"}
        return "false" if name == "aria-selected" else "local-row"
    row.get_attribute.side_effect = attribute
    page.get_by_role.return_value = message_list
    result = web.collect_outlook_rows()
    assert result["text_included"] is False
    assert result["rows"][0]["text_omitted"] is True
    assert result["rows"][0]["rendered_text"] == ""


def test_cloud_microsoft_redirect_is_an_exact_trusted_mail_origin() -> None:
    page = _page()
    page.url = "https://outlook.cloud.microsoft/mail/"
    assert web._wait_for_message_list(page, timeout=30) is page.get_by_role.return_value
    assert web._host("https://outlook.cloud.microsoft.evil.test/mail/") not in web._MAIL_HOSTS
