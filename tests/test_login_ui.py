"""현재 네이버 로그인 UI에 대한 단위 테스트.

2026-11-01 실제 실행에서 깨진 동작을 고정한다.
- 로그인 버튼은 #loginBtn_row / #loginBtn_column
- nid 페이지는 networkidle에 도달하지 않는다
- blog.naver.com 이 500이어도 NID_AUT+NID_SES 이면 로그인된 것이다
- 영수증 캡차 뒤 비밀번호가 비면 다시 채우되, 캡차 답과 과도한 자동 클릭은 사람에게 맡긴다
"""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from naver_blog_mcp.automation import login as login_mod
from naver_blog_mcp.automation.browser import new_browser_context
from naver_blog_mcp.automation.login import (
    assist_login_once,
    login_to_naver,
    start_login,
    verify_login_session,
)
from naver_blog_mcp.automation.selectors import NaverSelectors
from naver_blog_mcp.config import get_browser_config


class _Locator:
    def __init__(self, page: "FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_Locator":
        return self

    async def count(self) -> int:
        return 1 if self._selector in self._page.visible else 0

    async def is_visible(self) -> bool:
        return bool(self._page.visible.get(self._selector))

    async def click(self, timeout: int | None = None) -> None:
        if not await self.is_visible():
            raise PlaywrightTimeout(self._selector)
        self._page.focused = self._selector
        self._page.clicked.append(self._selector)

    async def text_content(self) -> str:
        return self._page.texts.get(self._selector, "")


class _Keyboard:
    def __init__(self, page: "FakePage") -> None:
        self._page = page

    async def type(self, text: str, delay: int = 0) -> None:
        self._page.typed.append(text)
        if self._page.focused:
            current = self._page.values.get(self._page.focused, "")
            self._page.values[self._page.focused] = current + text


class _Context:
    def __init__(self, page: "FakePage") -> None:
        self._page = page
        self.saved_path: str | None = None
        self.init_scripts: list[str] = []

    async def cookies(self) -> list[dict]:
        return list(self._page.cookies)

    async def storage_state(self, path: str | None = None) -> dict:
        self.saved_path = path
        return {}

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)


class FakePage:
    def __init__(self) -> None:
        self.visible: dict[str, bool] = {"#id": True, "#pw": True}
        self.values: dict[str, str] = {"#id": "", "#pw": ""}
        self.texts: dict[str, str] = {}
        self.cookies: list[dict] = []
        self.clicked: list[str] = []
        self.typed: list[str] = []
        self.focused: str | None = None
        self.goto_calls: list[dict] = []
        self.wait_calls: list[str] = []
        self.url = "about:blank"
        self.context = _Context(self)
        self.keyboard = _Keyboard(self)

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def goto(
        self, url: str, wait_until: str | None = None, timeout: int | None = None
    ) -> None:
        self.goto_calls.append(
            {"url": url, "wait_until": wait_until, "timeout": timeout}
        )
        self.url = url

    async def wait_for_selector(
        self, selector: str, timeout: int | None = None
    ) -> None:
        self.wait_calls.append(selector)
        if not self.visible.get(selector):
            raise PlaywrightTimeout(selector)

    async def click(self, selector: str, timeout: int | None = None) -> None:
        if not self.visible.get(selector):
            raise PlaywrightTimeout(selector)
        self.clicked.append(selector)

    async def input_value(self, selector: str) -> str:
        return self.values.get(selector, "")


class _Browser:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def new_context(self, **kwargs: object) -> _Context:
        self.kwargs = kwargs
        return _Context(FakePage())


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(login_mod, "_sleep", no_sleep)


def test_login_button_selectors_match_current_nid_markup() -> None:
    buttons = NaverSelectors.LOGIN["login_btn"]
    assert isinstance(buttons, list)
    assert buttons[:3] == [
        "#loginBtn_row:visible",
        "#loginBtn_column:visible",
        "button[data-nlog-area='login.loginbtn']:visible",
    ]


def test_browser_launch_drops_automation_flag() -> None:
    options = get_browser_config()
    assert "--disable-blink-features=AutomationControlled" in options["args"]
    assert options["ignore_default_args"] == ["--enable-automation"]


@pytest.mark.asyncio
async def test_stealth_context_hides_webdriver() -> None:
    browser = _Browser()
    context = await new_browser_context(browser)
    assert browser.kwargs is not None
    assert browser.kwargs["locale"] == "ko-KR"
    assert any("webdriver" in script for script in context.init_scripts)


@pytest.mark.asyncio
async def test_start_login_uses_domcontentloaded_and_current_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: dict = {}

    async def fake_assist(
        page: FakePage,
        user_id: str,
        password: str,
        initial_clicks: int = 0,
        **kwargs: object,
    ) -> bool:
        scheduled["user_id"] = user_id
        scheduled["initial_clicks"] = initial_clicks
        return False

    monkeypatch.setattr(login_mod, "run_login_assist", fake_assist)
    page = FakePage()
    page.visible["#loginBtn_row:visible"] = True

    await start_login(page, "naver-id", "secret")
    await asyncio.sleep(0)

    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"
    assert "networkidle" not in page.goto_calls[0]["wait_until"]
    assert "section.blog.naver.com" in page.goto_calls[0]["url"]
    assert page.wait_calls == ["#id"]
    assert page.typed == ["naver-id", "secret"]
    assert "#loginBtn_row:visible" in page.clicked
    assert scheduled["initial_clicks"] == 1
    assert scheduled["user_id"] == "naver-id"


@pytest.mark.asyncio
async def test_verify_trusts_nid_cookies_when_blog_home_errors() -> None:
    page = FakePage()
    page.cookies = [
        {"name": "NID_AUT", "domain": ".naver.com"},
        {"name": "NID_SES", "domain": ".naver.com"},
    ]

    async def goto_fails(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("blog.naver.com 500")

    page.goto = goto_fails  # type: ignore[method-assign]

    assert await verify_login_session(page) is True


@pytest.mark.asyncio
async def test_other_naver_cookies_do_not_count_as_login() -> None:
    page = FakePage()
    page.cookies = [{"name": "NNB", "domain": ".naver.com"}]

    assert await verify_login_session(page) is False
    assert page.goto_calls[0]["url"].startswith("https://section.blog.naver.com")
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"


@pytest.mark.asyncio
async def test_captcha_refill_does_not_click_or_answer() -> None:
    page = FakePage()
    page.visible["#captcha:visible"] = True

    clicks = await assist_login_once(page, "naver-id", "secret", 0)

    assert clicks == 0
    assert "#loginBtn_row:visible" not in page.clicked
    assert "button[data-nlog-area='login.loginbtn']:visible" not in page.clicked
    assert page.values["#pw"] == "secret"
    assert page.values["#id"] == "naver-id"


@pytest.mark.asyncio
async def test_auto_click_is_capped_when_password_keeps_getting_cleared() -> None:
    page = FakePage()
    page.visible["#loginBtn_row:visible"] = True
    clicks = 0

    for expected in (1, 2, 3):
        page.values["#pw"] = ""
        clicks = await assist_login_once(page, "naver-id", "secret", clicks)
        assert clicks == expected

    page.values["#pw"] = ""
    clicks = await assist_login_once(page, "naver-id", "secret", clicks)

    assert clicks == 3
    assert page.clicked.count("#loginBtn_row:visible") == 3
    assert page.values["#pw"] == "secret"


@pytest.mark.asyncio
async def test_filled_password_is_not_overwritten() -> None:
    page = FakePage()
    page.values["#id"] = "naver-id"
    page.values["#pw"] = "user-typed"

    clicks = await assist_login_once(page, "naver-id", "secret", 0)

    assert clicks == 0
    assert page.typed == []
    assert page.values["#pw"] == "user-typed"


@pytest.mark.asyncio
async def test_login_succeeds_from_cookies_without_opening_blog_home(
    tmp_path: Path,
) -> None:
    page = FakePage()
    page.visible["#loginBtn_row:visible"] = True

    async def click(selector: str, timeout: int | None = None) -> None:
        if not page.visible.get(selector):
            raise PlaywrightTimeout(selector)
        page.clicked.append(selector)
        page.cookies = [
            {"name": "NID_AUT", "domain": ".naver.com"},
            {"name": "NID_SES", "domain": ".naver.com"},
        ]

    page.click = click  # type: ignore[method-assign]
    storage = tmp_path / "auth.json"

    result = await login_to_naver(
        page,
        "naver-id",
        "secret",
        storage_state_path=str(storage),
        headless=True,
    )

    assert result["success"] is True
    assert page.context.saved_path == str(storage)
    assert page.goto_calls[0]["wait_until"] == "domcontentloaded"
    assert all("nid.naver.com" in call["url"] for call in page.goto_calls)
    assert all(
        call["url"].rstrip("/") != "https://blog.naver.com" for call in page.goto_calls
    )


@pytest.mark.asyncio
async def test_headless_captcha_is_reported() -> None:
    page = FakePage()
    page.visible["input[placeholder*='정답']:visible"] = True

    with pytest.raises(login_mod.CaptchaDetectedError):
        await login_to_naver(page, "naver-id", "secret", headless=True)
