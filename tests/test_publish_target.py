"""List target and publish confirmation.

section.blog.naver.com/BlogHome.naver was treated as a blog id because
the host check is a substring. The publish wait glob matches the current
postwrite URL, and the hashed final-publish selector can miss without
saying the confirm click never happened.
"""

import pytest

from naver_blog_mcp.automation import post_actions
from naver_blog_mcp.automation import read_actions
from naver_blog_mcp.automation.category_actions import get_categories
from naver_blog_mcp.automation.read_actions import _get_blog_id


SECTION_HOME = "https://section.blog.naver.com/BlogHome.naver"
EDITOR_URL = "https://blog.naver.com/kkforgg/postwrite"
PUBLISHED_URL = "https://blog.naver.com/kkforgg/22345678901"
FINAL_PUBLISH = "[class*='layer_popup'] button:text-is('발행')"
TOOLBAR_PUBLISH = "button:has-text('발행'):visible"


class _Locator:
    def __init__(self, page: "PubPage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> "_Locator":
        return self

    def nth(self, _index: int) -> "_Locator":
        return self

    async def is_visible(self) -> bool:
        return bool(self._page.visible.get(self._selector))

    async def count(self) -> int:
        return 1 if self._page.visible.get(self._selector) else 0

    async def click(self, timeout: int | None = None, force: bool = False) -> None:
        self._page.clicked.append(self._selector)
        if self._selector == FINAL_PUBLISH and self._page.publish_on_final_click:
            self._page.url = PUBLISHED_URL

    async def inner_text(self) -> str:
        return self._page.texts.get(self._selector, "")

    async def text_content(self) -> str:
        return await self.inner_text()


class PubPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.visible: dict[str, bool] = {}
        self.texts: dict[str, str] = {}
        self.clicked: list[str] = []
        self.gotos: list[str] = []
        self.frames = [self]
        self.publish_on_final_click = False

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    async def goto(
        self, url: str, wait_until: str | None = None, timeout: int | None = None
    ) -> None:
        self.gotos.append(url)
        self.url = url

    async def bring_to_front(self) -> None:
        return None

    async def title(self) -> str:
        return "editor"

    async def evaluate(self, _expression: str, *_args: object) -> str:
        return "No popup"

    async def wait_for_selector(
        self, selector: str, timeout: int | None = None
    ) -> None:
        raise TimeoutError(selector)

    async def wait_for_url(self, pattern: str, timeout: int | None = None) -> None:
        self.gotos.append(f"wait:{pattern}")
        return None

    async def screenshot(self, path: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(post_actions.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(post_actions, "_sleep", no_sleep)


def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_actions.config, "NAVER_BLOG_ID", "kkforgg")
    monkeypatch.setattr(post_actions, "_configured_blog_id", lambda: "kkforgg")


@pytest.mark.asyncio
async def test_section_bloghome_is_not_the_list_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    page = PubPage(SECTION_HOME)

    assert await _get_blog_id(page) == "kkforgg"


@pytest.mark.asyncio
async def test_categories_do_not_open_bloghome_as_a_blog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    page = PubPage(SECTION_HOME)

    result = await get_categories(page)

    assert result["success"] is False
    assert page.gotos
    assert page.gotos[0] == "https://blog.naver.com/kkforgg"
    assert "BlogHome" not in page.gotos[0]


@pytest.mark.asyncio
async def test_missing_final_publish_is_not_a_navigation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    page = PubPage(EDITOR_URL)
    page.visible[TOOLBAR_PUBLISH] = True

    result = await post_actions.publish_post(page, timeout=0)

    assert result["success"] is False
    assert result["status"] == "final_click_missing"
    assert result["post_url"] is None
    assert "페이지 이동에 실패" not in result["message"]
    assert "생성되지 않" not in result["message"]
    assert FINAL_PUBLISH not in page.clicked


@pytest.mark.asyncio
async def test_postwrite_url_is_not_publish_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    page = PubPage(EDITOR_URL)
    page.visible[TOOLBAR_PUBLISH] = True
    page.visible[FINAL_PUBLISH] = True

    result = await post_actions.publish_post(page, timeout=0)

    assert result["success"] is False
    assert result["status"] == "publish_unconfirmed"
    assert result["post_url"] is None
    assert FINAL_PUBLISH in page.clicked
    assert "확인되지 않았습니다" in result["message"]
    assert page.url == EDITOR_URL


@pytest.mark.asyncio
async def test_final_publish_click_reports_the_post_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    page = PubPage(EDITOR_URL)
    page.visible[TOOLBAR_PUBLISH] = True
    page.visible[FINAL_PUBLISH] = True
    page.publish_on_final_click = True

    result = await post_actions.publish_post(page, timeout=0)

    assert result["success"] is True
    assert result["status"] == "published"
    assert result["post_url"] == PUBLISHED_URL


@pytest.mark.asyncio
async def test_finalize_does_not_open_a_new_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    page = PubPage(SECTION_HOME)
    page.visible[TOOLBAR_PUBLISH] = True

    result = await post_actions.finalize_current_post(
        page,
        expected_title="고정 제목",
        visibility="전체공개",
    )

    assert result["success"] is False
    assert result["status"] == "not_on_editor"
    assert page.gotos == []
    assert page.clicked == []


@pytest.mark.asyncio
async def test_finalize_requires_the_open_editor_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(monkeypatch)
    page = PubPage(EDITOR_URL)
    page.visible[".se-documentTitle .se-text-paragraph"] = True
    page.texts[".se-documentTitle .se-text-paragraph"] = "다른 제목"
    page.visible[TOOLBAR_PUBLISH] = True
    page.visible[FINAL_PUBLISH] = True

    result = await post_actions.finalize_current_post(
        page,
        expected_title="고정 제목",
        visibility="전체공개",
        category="일상",
    )

    assert result["success"] is False
    assert result["status"] == "title_mismatch"
    assert page.gotos == []
    assert TOOLBAR_PUBLISH not in page.clicked
    assert "다른 제목" not in result["message"]
    assert "고정 제목" not in result["message"]
