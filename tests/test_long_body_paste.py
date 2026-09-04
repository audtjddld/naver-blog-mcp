"""장문 본문 입력 회귀 테스트."""

from typing import Any

import pytest

from naver_blog_mcp.automation import post_actions

CONTENT_SELECTOR = "div[contenteditable='true']:not([data-placeholder='제목'])"
LONG_BODY = "장문 본문입니다.\n" * 1000


class EditorStub:
    """스마트에디터 ONE의 direct contenteditable 경로를 흉내내는 스텁.

    paste_works=False면 붙여넣기 키 입력은 성공하지만 편집기가 반영하지 않는
    상황을 재현한다. 실제로 관찰된 실패 모드다.
    clear_works=False면 Select-All + Delete로도 편집기가 비워지지 않는 상황이다.
    """

    def __init__(self, paste_works: bool = True, clear_works: bool = True) -> None:
        self.paste_works = paste_works
        self.clear_works = clear_works
        self.clipboard: str = ""
        self.content: str = ""
        self.permissions: list[str] = []
        self.pressed: list[str] = []
        self.typed: list[str] = []
        self.context = _ContextStub(self)
        self.keyboard = _KeyboardStub(self)

    def locator(self, selector: str) -> "_LocatorStub":
        return _LocatorStub(self, selector)

    async def evaluate(self, expression: str, content: str) -> None:
        assert "navigator.clipboard.writeText" in expression
        self.clipboard = content


class _ContextStub:
    def __init__(self, editor: EditorStub) -> None:
        self._editor = editor

    async def grant_permissions(self, permissions: list[str]) -> None:
        self._editor.permissions = permissions


class _KeyboardStub:
    def __init__(self, editor: EditorStub) -> None:
        self._editor = editor

    async def press(self, key: str) -> None:
        self._editor.pressed.append(key)
        if key == "ControlOrMeta+V" and self._editor.paste_works:
            self._editor.content = self._editor.clipboard
        elif key == "Delete" and self._editor.clear_works:
            self._editor.content = ""

    async def type(self, content: str, delay: int) -> None:
        self._editor.typed.append(content)
        self._editor.content = content


class _LocatorStub:
    def __init__(self, editor: EditorStub, selector: str) -> None:
        self._editor = editor
        self._selector = selector

    @property
    def first(self) -> "_LocatorStub":
        return self

    async def count(self) -> int:
        return int(self._selector == CONTENT_SELECTOR)

    async def click(self) -> None:
        return None

    async def inner_text(self) -> str:
        return self._editor.content


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(post_actions.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_long_body_is_pasted_in_one_operation() -> None:
    editor: Any = EditorStub()

    await post_actions.fill_post_content(editor, LONG_BODY)

    assert editor.content == LONG_BODY
    assert editor.typed == [], "붙여넣기가 되면 keyboard.type을 쓰면 안 된다"
    assert editor.permissions == ["clipboard-read", "clipboard-write"]
    assert editor.pressed.count("ControlOrMeta+V") == 1


@pytest.mark.asyncio
async def test_unapplied_paste_falls_back_to_typing() -> None:
    """붙여넣기 키 입력이 성공해도 반영되지 않으면 폴백해야 한다.

    반영 여부를 확인하지 않고 True를 반환하면 본문이 빈 글로 저장된다.
    """
    editor: Any = EditorStub(paste_works=False)

    await post_actions.fill_post_content(editor, LONG_BODY)

    assert editor.typed == [LONG_BODY], "폴백으로 전체 본문이 입력돼야 한다"
    assert editor.content == LONG_BODY
    # 폴백 전에 편집기를 비워야 부분 붙여넣기 잔여물과 중복되지 않는다
    assert "Delete" in editor.pressed


@pytest.mark.asyncio
async def test_stale_content_with_matching_tail_does_not_pass_verification() -> None:
    """편집기에 같은 본문의 잔여물이 있어도 붙여넣기 성공으로 오판하면 안 된다.

    같은 글을 재시도하면 잔여 텍스트의 꼬리가 새 본문의 꼬리와 일치한다.
    꼬리 비교만으로는 통과하므로, 붙여넣기 전에 편집기를 비워야 한다.
    """
    editor: Any = EditorStub(paste_works=False)
    editor.content = LONG_BODY  # 이전 시도가 남긴 잔여물

    await post_actions.fill_post_content(editor, LONG_BODY)

    assert editor.typed == [LONG_BODY], "잔여물을 새 붙여넣기로 오인하면 안 된다"
    assert editor.content == LONG_BODY


@pytest.mark.asyncio
async def test_unclearable_editor_fails_loudly_instead_of_duplicating() -> None:
    """편집기를 비운 것을 확인할 수 없으면 폴백 입력도 안전하지 않다.

    그대로 type()을 실행하면 잔여물 위에 덧붙어 본문이 중복된다.
    """
    editor: Any = EditorStub(clear_works=False)
    editor.content = "이전 시도 잔여물"

    with pytest.raises(post_actions.NaverBlogPostError):
        await post_actions.fill_post_content(editor, LONG_BODY)

    assert editor.typed == [], "비우지 못했으면 입력을 시도하면 안 된다"
