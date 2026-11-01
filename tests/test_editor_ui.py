"""2026-11-01 스마트에디터 ONE 글쓰기 UI 단위 테스트.

본문은 iframe 안의 contenteditable 이 아니다. 보이는
`.se-text-paragraph` 에 입력해야 하고, 화면 밖 클립보드 헬퍼를 누르면
30초 타임아웃 뒤에 본문이 비워지지 않았다고 실패한다.
"""

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from naver_blog_mcp.automation import post_actions
from naver_blog_mcp.automation.post_actions import (
    create_blog_post,
    fill_post_content,
    fill_post_title,
    navigate_to_post_write_page,
)


class _Mouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    async def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))


class _Keyboard:
    def __init__(self, page: "EditorPage") -> None:
        self._page = page

    async def type(self, text: str, delay: int = 0) -> None:
        self._page.typed.append(text)

    async def insert_text(self, text: str) -> None:
        self._page.inserted.append(text)

    async def press(self, key: str) -> None:
        self._page.pressed.append(key)


class _Locator:
    def __init__(self, node: "Node", selector: str) -> None:
        self._node = node
        self._selector = selector

    @property
    def first(self) -> "_Locator":
        return self

    def nth(self, _index: int) -> "_Locator":
        return self

    async def count(self) -> int:
        return 1 if self._selector in self._node.visible else 0

    async def is_visible(self) -> bool:
        return bool(self._node.visible.get(self._selector))

    async def click(self, timeout: int | None = None) -> None:
        if not await self.is_visible():
            raise PlaywrightTimeout(self._selector)
        self._node.visible[self._selector] = False
        self._node.clicked.append(self._selector)
        self._node.page.clicked.append(self._selector)


class Node:
    def __init__(self, page: "EditorPage") -> None:
        self.page = page
        self.visible: dict[str, bool] = {}
        self.clicked: list[str] = []

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)


class EditorPage(Node):
    def __init__(self) -> None:
        super().__init__(self)
        self.frames: list[Node] = [self]
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse()
        self.typed: list[str] = []
        self.inserted: list[str] = []
        self.pressed: list[str] = []
        self.gotos: list[dict] = []
        self.url = "about:blank"
        self.screenshots: list[str] = []

    async def goto(
        self, url: str, wait_until: str | None = None, timeout: int | None = None
    ) -> None:
        self.gotos.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        self.url = url

    async def bring_to_front(self) -> None:
        return None

    async def screenshot(self, path: str) -> None:
        self.screenshots.append(path)

    async def click(self, selector: str, timeout: int | None = None) -> None:
        await self.locator(selector).click(timeout=timeout)

    async def title(self) -> str:
        return "글쓰기"

    async def evaluate(self, _expression: str, *_args: object) -> None:
        return None

    async def wait_for_url(self, _pattern: str, timeout: int | None = None) -> None:
        self.url = "https://blog.naver.com/kkforgg/123"


def _ready_editor() -> EditorPage:
    page = EditorPage()
    page.visible[".se-documentTitle .se-text-paragraph"] = True
    page.visible[".se-component.se-text .se-text-paragraph"] = True
    page.visible["button[class*='save_btn']"] = True
    return page


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(post_actions.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_postwrite_navigation_skips_blog_home() -> None:
    page = _ready_editor()

    await navigate_to_post_write_page(page, blog_id="kkforgg")

    assert page.gotos[0]["url"] == "https://blog.naver.com/kkforgg/postwrite"
    assert page.gotos[0]["wait_until"] == "domcontentloaded"
    assert all(
        call["url"].rstrip("/") != "https://blog.naver.com" for call in page.gotos
    )


@pytest.mark.asyncio
async def test_title_uses_visible_document_title_not_coordinates() -> None:
    page = _ready_editor()

    await fill_post_title(page, "오늘의 제목")

    assert page.typed == ["오늘의 제목"]
    assert page.mouse.clicks == []
    assert ".se-documentTitle .se-text-paragraph" in page.clicked


@pytest.mark.asyncio
async def test_title_is_found_in_a_child_frame() -> None:
    page = EditorPage()
    child = Node(page)
    child.visible[".se-documentTitle .se-text-paragraph"] = True
    page.frames = [page, child]

    await fill_post_title(page, "프레임 제목")

    assert child.clicked == [".se-documentTitle .se-text-paragraph"]
    assert page.typed == ["프레임 제목"]
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_continue_draft_popup_is_cancelled_not_confirmed() -> None:
    page = _ready_editor()
    page.visible["button:has-text('확인')"] = True
    page.visible[".se-popup-button-cancel"] = True

    await fill_post_title(page, "제목")

    assert page.clicked[0] == ".se-popup-button-cancel"
    assert "button:has-text('확인')" not in page.clicked
    assert page.typed == ["제목"]


@pytest.mark.asyncio
async def test_body_skips_hidden_clipboard_helper() -> None:
    page = EditorPage()
    page.visible["div[contenteditable='true']:not([data-placeholder='제목'])"] = False
    page.visible[".se-component.se-text .se-text-paragraph"] = True

    await fill_post_content(page, "한 줄\n두 줄")

    assert (
        "div[contenteditable='true']:not([data-placeholder='제목'])" not in page.clicked
    )
    assert page.inserted == ["한 줄", "두 줄"]
    assert page.pressed == ["Enter"]
    assert page.typed == []


@pytest.mark.asyncio
async def test_blank_lines_become_enter_without_insert() -> None:
    page = EditorPage()
    page.visible[".se-component.se-text .se-text-paragraph"] = True

    await fill_post_content(page, "위\n\n아래")

    assert page.inserted == ["위", "아래"]
    assert page.pressed == ["Enter", "Enter"]


@pytest.mark.asyncio
async def test_url_ending_line_does_not_stay_a_bare_url() -> None:
    page = EditorPage()
    page.visible[".se-component.se-text .se-text-paragraph"] = True

    await fill_post_content(page, "설명 https://example.com/a\n다음 줄")

    assert page.inserted[0].startswith("설명 https://example.com/a")
    assert not page.inserted[0].endswith("https://example.com/a")
    assert page.inserted[1] == "다음 줄"
    assert page.pressed == ["Enter"]


@pytest.mark.asyncio
async def test_draft_saves_with_current_save_button() -> None:
    page = _ready_editor()

    result = await create_blog_post(
        page,
        "제목",
        "본문 한 줄",
        blog_id="kkforgg",
        draft=True,
    )

    assert result["success"] is True
    assert page.gotos[0]["url"] == "https://blog.naver.com/kkforgg/postwrite"
    assert page.gotos[0]["wait_until"] == "domcontentloaded"
    assert page.typed == ["제목"]
    assert page.inserted == ["본문 한 줄"]
    assert "button[class*='save_btn']" in page.clicked
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_publish_true_does_not_click_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: dict = {}

    async def fake_publish(
        page: EditorPage,
        wait_for_completion: bool = True,
        timeout: int = 30000,
        category: str | None = None,
        visibility: str | None = None,
    ) -> dict:
        published["visibility"] = visibility
        published["category"] = category
        return {
            "success": True,
            "message": "발행",
            "post_url": "https://blog.naver.com/kkforgg/1",
        }

    monkeypatch.setattr(post_actions, "publish_post", fake_publish)
    page = _ready_editor()

    result = await create_blog_post(
        page,
        "제목",
        "본문",
        blog_id="kkforgg",
        draft=False,
        visibility="비공개",
    )

    assert result["post_url"] == "https://blog.naver.com/kkforgg/1"
    assert published["visibility"] == "비공개"
    assert "button[class*='save_btn']" not in page.clicked
