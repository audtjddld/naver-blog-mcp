"""Regression tests for MCP login session ownership.

These fixtures reproduce the failed confirm/login flow without a live
Naver account:

- a NID_SES-only session was treated as logged out, and confirm moved the
  MCP page to section.blog.naver.com
- a visible 2FA/captcha challenge was still marked authenticated when
  stale auth cookies existed
- IP Security stayed on while credentials were submitted
- an English Sign in control was missed, and empty fields were reported
  as submitted
- the editor redirecting to a different blog was still typed into
- an older storage file was ignored, and a failed check could be treated
  as a reason to drop a loaded session
"""

import json
import os
import time
from pathlib import Path

import pytest
import pytest_asyncio
from playwright.async_api import TimeoutError as PlaywrightTimeout

from naver_blog_mcp.automation import login as login_mod
from naver_blog_mcp.automation import post_actions
from naver_blog_mcp.automation.login import assist_login_once, start_login
from naver_blog_mcp.config import Config
from naver_blog_mcp.mcp.tools import (
    handle_confirm_login,
    handle_create_post,
    handle_login,
)
from naver_blog_mcp.services.session_manager import SessionManager


class _Locator:
    def __init__(self, page: "FlowPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_Locator":
        return self

    def nth(self, _index: int) -> "_Locator":
        return self

    async def count(self) -> int:
        return 1 if self._page.visible.get(self._selector) else 0

    async def is_visible(self) -> bool:
        return bool(self._page.visible.get(self._selector))

    async def get_attribute(self, name: str) -> str | None:
        return self._page.attrs.get(self._selector, {}).get(name)

    async def click(self, timeout: int | None = None) -> None:
        if not await self.is_visible():
            raise PlaywrightTimeout(self._selector)
        self._page.focused = self._selector
        self._page.clicked.append(self._selector)
        self._page.events.append(("click", self._selector))
        attrs = self._page.attrs.get(self._selector)
        if attrs and attrs.get("aria-checked") == "true":
            attrs["aria-checked"] = "false"

    async def text_content(self) -> str:
        return ""


class _Keyboard:
    def __init__(self, page: "FlowPage", stick: bool = True) -> None:
        self._page = page
        self._stick = stick

    async def type(self, text: str, delay: int = 0) -> None:
        self._page.typed.append(text)
        self._page.events.append(("type", text))
        if self._stick and self._page.focused:
            current = self._page.values.get(self._page.focused, "")
            self._page.values[self._page.focused] = current + text

    async def press(self, key: str) -> None:
        self._page.pressed.append(key)

    async def insert_text(self, text: str) -> None:
        self._page.inserted.append(text)


class _Context:
    def __init__(self, page: "FlowPage") -> None:
        self._page = page
        self.saved_path: str | None = None

    async def cookies(self, urls: list[str] | None = None) -> list[dict]:
        if urls is None:
            return [
                cookie for cookie in self._page.cookies if not cookie.get("url_only")
            ]
        return list(self._page.cookies)

    async def storage_state(self, path: str | None = None) -> dict:
        self.saved_path = path
        if path:
            Path(path).write_text("{}", encoding="utf-8")
        return {"cookies": []}

    async def new_page(self) -> "FlowPage":
        return self._page


class FlowPage:
    def __init__(self, *, stick: bool = True) -> None:
        self.visible: dict[str, bool] = {"#id": True, "#pw": True}
        self.values: dict[str, str] = {"#id": "", "#pw": ""}
        self.attrs: dict[str, dict[str, str]] = {}
        self.cookies: list[dict] = []
        self.clicked: list[str] = []
        self.typed: list[str] = []
        self.inserted: list[str] = []
        self.pressed: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.focused: str | None = None
        self.goto_calls: list[str] = []
        self.url = "https://nid.naver.com/nidlogin.login"
        self.context = _Context(self)
        self.keyboard = _Keyboard(self, stick=stick)
        self.frames = [self]

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def goto(
        self, url: str, wait_until: str | None = None, timeout: int | None = None
    ) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_selector(
        self, selector: str, timeout: int | None = None
    ) -> None:
        if not self.visible.get(selector):
            raise PlaywrightTimeout(selector)

    async def click(self, selector: str, timeout: int | None = None) -> None:
        await self.locator(selector).click(timeout=timeout)

    async def input_value(self, selector: str) -> str:
        return self.values.get(selector, "")

    async def bring_to_front(self) -> None:
        return None

    async def screenshot(self, path: str) -> None:
        return None

    def is_closed(self) -> bool:
        return False


def _ip_off(page: FlowPage, selector: str = "label.switch_btn") -> None:
    page.visible[selector] = True
    page.attrs[selector] = {"aria-checked": "false"}


def _login_button(page: FlowPage) -> None:
    page.visible["#loginBtn_row:visible"] = True


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(login_mod, "_sleep", no_sleep)
    monkeypatch.setattr(post_actions, "_sleep", no_sleep)


@pytest_asyncio.fixture(autouse=True)
async def _cancel_assist():
    yield
    tasks = list(login_mod._assist_tasks)
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except Exception:
            pass


@pytest.mark.asyncio
async def test_old_storage_file_is_still_eligible_for_reuse(tmp_path: Path) -> None:
    storage = tmp_path / "auth.json"
    storage.write_text('{"cookies":[{"name":"NID_SES"}]}', encoding="utf-8")
    old = time.time() - (48 * 60 * 60)
    os.utime(storage, (old, old))

    manager = SessionManager(
        "login-user",
        "test-password",
        storage_path=str(storage),
        session_validity_hours=24,
    )

    assert manager.is_session_file_valid() is True
    assert storage.exists()


@pytest.mark.asyncio
async def test_unknown_probe_keeps_loaded_session_and_file() -> None:
    from naver_blog_mcp.services.session_manager import session_reuse_action

    assert (
        session_reuse_action({"logged_in": True, "status": "authenticated"}) == "reuse"
    )
    assert session_reuse_action({"logged_in": False, "status": "unknown"}) == "reuse"
    assert (
        session_reuse_action({"logged_in": False, "status": "logged_out"})
        == "drop_memory"
    )
    assert (
        session_reuse_action({"logged_in": False, "status": "awaiting_2fa"})
        == "drop_memory"
    )


@pytest.mark.asyncio
async def test_login_id_and_publish_blog_are_different_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NAVER_LOGIN_ID", "login-user")
    monkeypatch.setenv("NAVER_PUBLISH_BLOG_ID", "kkforgg")
    monkeypatch.setenv("NAVER_BLOG_PASSWORD", "test-password")
    monkeypatch.delenv("NAVER_BLOG_ID", raising=False)

    import naver_blog_mcp.config as config_mod

    assert config_mod.login_id() == "login-user"
    assert config_mod.publish_blog_id() == "kkforgg"
    Config.validate()


@pytest.mark.asyncio
async def test_preferred_page_is_the_pinned_login_page() -> None:
    from naver_blog_mcp.server import preferred_page

    login_page = object()
    other_page = object()

    assert preferred_page([other_page, login_page], login_page) is login_page
    assert preferred_page([other_page], None) is other_page


@pytest.mark.asyncio
async def test_nid_ses_session_is_saved_without_leaving_the_login_browser(
    tmp_path: Path,
) -> None:
    page = FlowPage()
    page.url = "https://www.naver.com/"
    page.cookies = [
        {"name": "NID_SES", "value": "secret-session", "domain": ".naver.com"}
    ]
    storage = tmp_path / "auth.json"

    result = await handle_confirm_login(page, page.context, str(storage))

    assert result["success"] is True
    assert result["logged_in"] is True
    assert page.goto_calls == []
    assert page.url == "https://www.naver.com/"
    assert page.context.saved_path == str(storage)
    assert "secret-session" not in json.dumps(result)


@pytest.mark.asyncio
async def test_url_scoped_auth_cookie_counts_without_navigation(tmp_path: Path) -> None:
    page = FlowPage()
    page.url = "https://www.naver.com/"
    page.cookies = [
        {
            "name": "NID_AUT",
            "value": "secret-auth",
            "domain": ".naver.com",
            "url_only": True,
        }
    ]
    storage = tmp_path / "auth.json"

    result = await handle_confirm_login(page, page.context, str(storage))

    assert result["logged_in"] is True
    assert page.goto_calls == []
    assert "secret-auth" not in json.dumps(result)


@pytest.mark.asyncio
async def test_two_factor_is_not_marked_authenticated(tmp_path: Path) -> None:
    page = FlowPage()
    page.url = "https://nid.naver.com/nidlogin.login"
    page.visible["text=2단계 인증"] = True
    page.cookies = [
        {"name": "NID_AUT", "value": "secret-auth", "domain": ".naver.com"},
        {"name": "NID_SES", "value": "secret-session", "domain": ".naver.com"},
    ]
    storage = tmp_path / "auth.json"
    storage.write_text("original-session", encoding="utf-8")

    result = await handle_confirm_login(page, page.context, str(storage))

    assert result["success"] is False
    assert result["logged_in"] is False
    assert result["status"] == "awaiting_2fa"
    assert page.goto_calls == []
    assert storage.read_text(encoding="utf-8") == "original-session"
    blob = json.dumps(result)
    assert "secret-auth" not in blob
    assert "secret-session" not in blob


@pytest.mark.asyncio
async def test_logged_out_confirm_does_not_move_or_erase_storage(
    tmp_path: Path,
) -> None:
    page = FlowPage()
    page.url = "https://nid.naver.com/nidlogin.login"
    page.cookies = [{"name": "NNB", "value": "tracker", "domain": ".naver.com"}]
    storage = tmp_path / "auth.json"
    storage.write_text("keep-existing-session", encoding="utf-8")

    result = await handle_confirm_login(page, page.context, str(storage))

    assert result["logged_in"] is False
    assert page.goto_calls == []
    assert page.url == "https://nid.naver.com/nidlogin.login"
    assert storage.read_text(encoding="utf-8") == "keep-existing-session"
    assert "tracker" not in json.dumps(result)


@pytest.mark.asyncio
async def test_ip_security_is_turned_off_before_credentials() -> None:
    page = FlowPage()
    _login_button(page)
    page.visible["label.switch_btn"] = True
    page.attrs["label.switch_btn"] = {"aria-checked": "true"}

    result = await handle_login(page, "login-user", "test-password")

    assert result.get("credentials_submitted") is True
    assert result.get("ip_security") == "turned_off"
    assert result.get("status") != "login_started"
    switch_at = page.events.index(("click", "label.switch_btn"))
    typed_at = page.events.index(("type", "login-user"))
    assert switch_at < typed_at
    assert page.attrs["label.switch_btn"]["aria-checked"] == "false"
    assert "test-password" not in json.dumps(result)
    assert "성공" not in result["message"]


@pytest.mark.asyncio
async def test_english_ip_security_label_is_turned_off_before_submit() -> None:
    page = FlowPage()
    _login_button(page)
    selector = "label:has-text('IP Security')"
    page.visible[selector] = True
    page.attrs[selector] = {"aria-checked": "true"}

    result = await handle_login(page, "login-user", "test-password")

    assert result.get("ip_security") == "turned_off"
    assert ("click", selector) in page.events
    assert page.events.index(("click", selector)) < page.events.index(
        ("type", "login-user")
    )


@pytest.mark.asyncio
async def test_empty_fields_are_not_reported_as_submitted() -> None:
    page = FlowPage(stick=False)
    _ip_off(page)
    _login_button(page)

    result = await handle_login(page, "login-user", "test-password")

    assert result["success"] is False
    assert result.get("credentials_submitted") is False
    assert result.get("status") == "credentials_not_entered"
    assert "#loginBtn_row:visible" not in page.clicked
    assert "제출" not in result["message"]
    assert "test-password" not in json.dumps(result)


@pytest.mark.asyncio
async def test_english_sign_in_anchor_submits_credentials() -> None:
    page = FlowPage()
    _ip_off(page)
    page.visible["a:has-text('Sign in')"] = True

    result = await handle_login(page, "login-user", "test-password")

    assert result.get("credentials_submitted") is True
    assert "a:has-text('Sign in')" in page.clicked
    assert "test-password" not in json.dumps(result)


@pytest.mark.asyncio
async def test_two_factor_screen_is_not_auto_clicked() -> None:
    page = FlowPage()
    page.url = "https://nid.naver.com/nidlogin.login"
    page.visible["text=2단계 인증"] = True
    _login_button(page)

    clicks = await assist_login_once(page, "login-user", "test-password", 0)

    assert clicks == 0
    assert "#loginBtn_row:visible" not in page.clicked
    assert page.typed == []


@pytest.mark.asyncio
async def test_missing_ip_switch_does_not_submit_credentials() -> None:
    page = FlowPage()
    _login_button(page)

    result = await start_login(page, "login-user", "test-password")

    assert result["credentials_submitted"] is False
    assert result["status"] == "ip_security_not_confirmed_off"
    assert page.typed == []
    assert "#loginBtn_row:visible" not in page.clicked


@pytest.mark.asyncio
async def test_create_post_stops_when_editor_opens_a_different_blog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(post_actions, "_configured_blog_id", lambda: "akchaknote")
    page = FlowPage()
    page.visible[".se-documentTitle .se-text-paragraph"] = True
    page.visible[".se-component.se-text .se-text-paragraph"] = True
    page.visible["button[class*='save_btn']"] = True

    async def goto(
        url: str, wait_until: str | None = None, timeout: int | None = None
    ) -> None:
        page.goto_calls.append(url)
        page.url = "https://blog.naver.com/kkforgg/postwrite"

    page.goto = goto  # type: ignore[method-assign]

    result = await handle_create_post(page, title="제목", content="본문", publish=False)

    assert result["success"] is False
    assert "kkforgg" in result["message"]
    assert "akchaknote" in result["message"]
    assert page.typed == []
    assert page.inserted == []
