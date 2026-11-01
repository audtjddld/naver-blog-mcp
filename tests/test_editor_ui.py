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
        page = self._node.page
        page.click_timeouts.append((self._selector, timeout))
        hook = page.before_click
        if hook is not None:
            await hook(self._selector, timeout)
        if not await self.is_visible():
            raise PlaywrightTimeout(self._selector)
        self._node.visible[self._selector] = False
        self._node.clicked.append(self._selector)
        if page is not self._node:
            page.clicked.append(self._selector)


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
        self.click_timeouts: list[tuple[str, int | None]] = []
        self.before_click = None

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
async def test_late_continue_popup_is_cancelled_before_title_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이어쓰기 모달은 제목 노드보다 늦게 뜬다.

    제목이 보인 직후 한 번만 닫으면 모달이 아직 없어, 이후 클릭이
    se-container에 막혀 30초를 기다린다.
    """
    page = _ready_editor()
    revealed = {"done": False}
    sleeps = {"n": 0}

    async def sleep_and_reveal(_seconds: float = 0) -> None:
        sleeps["n"] += 1
        if not revealed["done"] and sleeps["n"] >= 2:
            revealed["done"] = True
            page.visible[".se-popup-button-cancel"] = True
            page.visible["button:has-text('확인')"] = True

    monkeypatch.setattr(post_actions, "_sleep", sleep_and_reveal)

    await fill_post_title(page, "제목")

    assert ".se-popup-button-cancel" in page.clicked
    assert "button:has-text('확인')" not in page.clicked
    assert page.typed == ["제목"]
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_intercepted_title_click_retries_after_cancel_without_30s_timeout() -> (
    None
):
    page = _ready_editor()
    page.visible["button:has-text('확인')"] = True
    intercepted = {"done": False}

    async def intercept(selector: str, timeout: int | None) -> None:
        if selector != ".se-documentTitle .se-text-paragraph" or intercepted["done"]:
            return
        intercepted["done"] = True
        page.visible[".se-popup-button-cancel"] = True
        raise PlaywrightTimeout(
            f'{selector} <div class="se-container"> intercepts pointer events'
        )

    page.before_click = intercept

    await fill_post_title(page, "제목")

    title_timeouts = [
        timeout
        for selector, timeout in page.click_timeouts
        if selector == ".se-documentTitle .se-text-paragraph"
    ]
    assert title_timeouts
    assert all(timeout is not None and timeout < 30000 for timeout in title_timeouts)
    assert ".se-popup-button-cancel" in page.clicked
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
async def test_url_line_is_inserted_unchanged_before_the_next_line() -> None:
    """URL 줄 끝에 문자를 붙이면 접두어가 지워지고 주소가 다음 줄 끝으로 붙는다."""
    page = EditorPage()
    page.visible[".se-component.se-text .se-text-paragraph"] = True
    body = (
        "MCP 수정 후 동작 확인용 임시저장 글입니다.\n"
        "둘째 줄입니다.\n"
        "출처 예시: https://www.cdc.gov/ncbddd/actearly/milestones/milestones-15mo.html\n"
        "링크 다음 줄이 따로 떨어져 있어야 합니다."
    )

    await fill_post_content(page, body)

    assert page.inserted == [
        "MCP 수정 후 동작 확인용 임시저장 글입니다.",
        "둘째 줄입니다.",
        "출처 예시: https://www.cdc.gov/ncbddd/actearly/milestones/milestones-15mo.html",
        "링크 다음 줄이 따로 떨어져 있어야 합니다.",
    ]
    assert "\u200b" not in "".join(page.inserted)
    assert page.pressed == ["Enter", "Enter", "Enter"]


@pytest.mark.asyncio
async def test_dismiss_does_not_click_strikethrough_labeled_cancel() -> None:
    """button:has-text('취소') 는 툴바 '취소선' 버튼까지 눌러 본문 전체에 취소선을 켠다."""
    page = EditorPage()
    page.visible[".se-component.se-text .se-text-paragraph"] = True
    page.visible["button:has-text('취소')"] = True

    await fill_post_content(page, "둘째 줄입니다.")

    assert "button:has-text('취소')" not in page.clicked
    assert page.inserted == ["둘째 줄입니다."]


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
