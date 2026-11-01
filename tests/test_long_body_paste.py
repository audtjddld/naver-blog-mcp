"""장문 본문은 줄 단위 insert_text 로 넣는다.

한 글자씩 type 하면 순서가 뒤섞이고, 화면 밖 contenteditable(클립보드 헬퍼)에
붙여넣으면 편집기를 비우지 못했다고 실패한다. 보이는 본문 문단에 한 줄씩 넣는다.
"""

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from naver_blog_mcp.automation import post_actions
from naver_blog_mcp.automation.post_actions import fill_post_content

LONG_BODY = "장문 본문입니다.\n" * 1000
BODY_SELECTOR = ".se-component.se-text .se-text-paragraph"
HELPER_SELECTOR = "div[contenteditable='true']:not([data-placeholder='제목'])"


class _Keyboard:
    def __init__(self, page: "PageStub") -> None:
        self._page = page

    async def type(self, text: str, delay: int = 0) -> None:
        self._page.typed.append(text)

    async def insert_text(self, text: str) -> None:
        self._page.inserted.append(text)

    async def press(self, key: str) -> None:
        self._page.pressed.append(key)


class _Locator:
    def __init__(self, page: "PageStub", selector: str) -> None:
        self._page = page
        self._selector = selector

    def nth(self, _index: int) -> "_Locator":
        return self

    async def count(self) -> int:
        return 1 if self._selector in self._page.visible else 0

    async def is_visible(self) -> bool:
        return bool(self._page.visible.get(self._selector))

    async def click(self, timeout: int | None = None) -> None:
        if not await self.is_visible():
            raise PlaywrightTimeout(self._selector)
        self._page.clicked.append(self._selector)


class PageStub:
    def __init__(self) -> None:
        self.visible = {BODY_SELECTOR: True}
        self.clicked: list[str] = []
        self.typed: list[str] = []
        self.inserted: list[str] = []
        self.pressed: list[str] = []
        self.keyboard = _Keyboard(self)
        self.frames = [self]

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(post_actions.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_long_body_is_inserted_line_by_line() -> None:
    page = PageStub()

    await fill_post_content(page, LONG_BODY)

    assert page.inserted == ["장문 본문입니다."] * 1000
    assert page.pressed == ["Enter"] * 1000
    assert page.typed == []
    assert HELPER_SELECTOR not in page.clicked


@pytest.mark.asyncio
async def test_hidden_clipboard_helper_is_not_a_body_target() -> None:
    page = PageStub()
    page.visible = {HELPER_SELECTOR: False, BODY_SELECTOR: True}

    await fill_post_content(page, "본문")

    assert page.clicked == [BODY_SELECTOR]
    assert page.inserted == ["본문"]


@pytest.mark.asyncio
async def test_hidden_helper_alone_fails_without_typing() -> None:
    """헬퍼만 보이면(실제로는 액션 불가) 그 위에 입력하지 않고 실패한다."""
    page = PageStub()
    page.visible = {HELPER_SELECTOR: False}

    with pytest.raises(post_actions.NaverBlogPostError):
        await fill_post_content(page, LONG_BODY)

    assert page.typed == []
    assert page.inserted == []
