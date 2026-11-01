"""네이버 블로그 글쓰기 자동화."""

import asyncio
import logging
import weakref
from typing import Optional, Dict, Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .selectors import (
    POST_WRITE_TITLE,
    POST_WRITE_CONTENT_BODY,
    POST_WRITE_PUBLISH_BTN,
    NaverSelectors,
)


logger = logging.getLogger(__name__)

# '작성 중인 글이 있습니다'는 제목 노드보다 늦게 붙는다. 그 전에 한 번만 보면 놓친다.
CONTINUE_POPUP_WAIT_S = 6.0
CONTINUE_POPUP_POLL_S = 0.5
# 기본 30초는 모달이 클릭을 가로막을 때 그대로 낭비된다.
EDITOR_CLICK_TIMEOUT_MS = 5000
_late_popup_waited: "weakref.WeakKeyDictionary[object, bool]" = (
    weakref.WeakKeyDictionary()
)


class NaverBlogPostError(Exception):
    """네이버 블로그 글쓰기 관련 에러."""

    pass


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _configured_blog_id() -> Optional[str]:
    """로그인 아이디와 같은 블로그 아이디. 없으면 None."""
    try:
        from ..config import config

        blog_id = (config.NAVER_BLOG_ID or "").strip()
    except Exception:
        return None
    return blog_id or None


def _frames_of(page: Page) -> list:
    frames = getattr(page, "frames", None)
    if frames:
        return list(frames)
    return [page]


def _as_selectors(selectors: object) -> list[str]:
    if isinstance(selectors, str):
        return [selectors]
    return list(selectors)  # type: ignore[arg-type]


async def find_visible(page: Page, selectors: object):
    """모든 프레임에서 보이는 첫 요소를 찾는다.

    Returns:
        (frame, locator, selector) 또는 없으면 (None, None, None).
    """
    for frame in _frames_of(page):
        for selector in _as_selectors(selectors):
            try:
                locator = frame.locator(selector)
                count = await locator.count()
                for index in range(min(count, 5)):
                    candidate = locator.nth(index)
                    if await candidate.is_visible():
                        return frame, candidate, selector
            except Exception:
                continue
    return None, None, None


def _click_blocked(exc: BaseException) -> bool:
    """모달이 포인터를 가로채면 Playwright는 타임아웃까지 클릭을 붙잡는다."""
    if isinstance(exc, PlaywrightTimeout):
        return True
    return "intercepts pointer events" in str(exc)


def _forget_continue_popup(page: Page) -> None:
    try:
        _late_popup_waited.pop(page, None)
    except TypeError:
        return


def _mark_continue_popup_waited(page: Page) -> None:
    try:
        _late_popup_waited[page] = True
    except TypeError:
        return


def _continue_popup_waited(page: Page) -> bool:
    try:
        return bool(_late_popup_waited.get(page))
    except TypeError:
        return False


async def dismiss_write_popups(page: Page, rounds: int = 2) -> bool:
    """'작성 중인 글 이어쓰기'는 취소해야 새 글에 입력된다.

    확인 버튼은 이어쓰기를 수락하므로 누르지 않는다.
    보이는 취소 버튼을 눌렀으면 True.
    """
    selectors = NaverSelectors.POST_WRITE["popup_dismiss"]
    dismissed = False
    for _ in range(rounds):
        _frame, element, selector = await find_visible(page, selectors)
        if element is None:
            return dismissed
        try:
            await element.click(timeout=EDITOR_CLICK_TIMEOUT_MS)
            dismissed = True
            logger.info("글쓰기 팝업 닫기(취소): %s", selector)
        except Exception as exc:
            logger.info("글쓰기 팝업 닫기 실패(무시): %s", exc)
            return dismissed
        await _sleep(0.2)
    return dismissed


async def wait_for_continue_draft_popup(
    page: Page, timeout_s: float = CONTINUE_POPUP_WAIT_S
) -> bool:
    """제목이 보인 뒤에 뜨는 이어쓰기 모달을 잠시 기다렸다가 취소한다.

    모달이 보이면 바로 취소하고, 아직 없으면 timeout_s 까지 다시 본다.
    """
    polls = max(1, int(timeout_s / CONTINUE_POPUP_POLL_S))
    for _ in range(polls):
        if await dismiss_write_popups(page):
            await _sleep(0.3)
            await dismiss_write_popups(page)
            return True
        await _sleep(CONTINUE_POPUP_POLL_S)
    return False


async def ensure_continue_popup_cleared(page: Page) -> None:
    """글마다 한 번은 늦은 모달을 기다리고, 그 다음 클릭 전에는 다시 닫기만 한다."""
    if _continue_popup_waited(page):
        await dismiss_write_popups(page)
        return
    await wait_for_continue_draft_popup(page)
    _mark_continue_popup_waited(page)


async def click_editor_target(page: Page, selectors: object, *, what: str) -> str:
    """보이는 편집 영역을 짧게 클릭한다.

    모달이 포인터를 가로채면 30초를 기다리지 않고 취소를 누른 뒤 다시 클릭한다.
    """
    last_error: Optional[BaseException] = None
    for _ in range(3):
        await dismiss_write_popups(page)
        _frame, element, selector = await find_visible(page, selectors)
        if element is None:
            raise NaverBlogPostError(f"{what} 입력란을 찾을 수 없습니다.")
        try:
            await element.click(timeout=EDITOR_CLICK_TIMEOUT_MS)
            return selector
        except Exception as exc:
            if not _click_blocked(exc):
                raise
            last_error = exc
            logger.info("%s 클릭이 팝업에 가려 취소 후 재시도합니다.", what)
    raise NaverBlogPostError(f"{what} 클릭이 이어쓰기 팝업에 막혔습니다: {last_error}")


async def navigate_to_post_write_page(
    page: Page, blog_id: Optional[str] = None, timeout: int = 30000
) -> None:
    """
    네이버 블로그 글쓰기 페이지로 이동합니다.

    Args:
        page: Playwright Page 객체
        blog_id: 블로그 ID (옵션, 없으면 자동으로 현재 로그인된 블로그 사용)
        timeout: 페이지 로딩 대기 시간 (ms)

    Raises:
        NaverBlogPostError: 페이지 이동 실패 시
    """
    try:
        # blog.naver.com 루트는 500이 잦다. 글쓰기 URL로 바로 간다.
        if isinstance(blog_id, str):
            blog_id = blog_id.strip() or None
        if not blog_id:
            blog_id = _configured_blog_id()
        if blog_id:
            url = f"https://blog.naver.com/{blog_id}/postwrite"
        else:
            url = "https://blog.naver.com/postwrite"

        _forget_continue_popup(page)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

        for _ in range(8):
            _frame, element, _selector = await find_visible(page, POST_WRITE_TITLE)
            if element is not None:
                break
            await _sleep(1)

        current_url = page.url
        logger.info("글쓰기 페이지 URL: %s", current_url)
        if (
            "postwrite" in current_url.lower()
            or "PostWriteForm" in current_url
            or "Redirect=Write" in current_url
        ):
            return

        _frame, element, selector = await find_visible(page, POST_WRITE_TITLE)
        if element is not None:
            logger.info("제목 입력란 발견: %s", selector)
            return

        raise NaverBlogPostError(
            f"글쓰기 페이지 로딩에 실패했습니다. 현재 URL: {current_url}"
        )

    except NaverBlogPostError:
        raise
    except PlaywrightTimeout as e:
        raise NaverBlogPostError(f"글쓰기 페이지 이동 시간 초과: {str(e)}")
    except Exception as e:
        raise NaverBlogPostError(f"글쓰기 페이지 이동 중 오류: {str(e)}")


async def fill_post_title(page: Page, title: str) -> None:
    """
    블로그 글 제목을 입력합니다.

    Args:
        page: Playwright Page 객체
        title: 글 제목

    Raises:
        NaverBlogPostError: 제목 입력 실패 시
    """
    try:
        await ensure_continue_popup_cleared(page)
        selector = await click_editor_target(page, POST_WRITE_TITLE, what="제목")
        await _sleep(0.3)
        await page.keyboard.type(title, delay=20)
        logger.info("제목 입력 완료: %s (%s)", title, selector)
        await _sleep(0.3)
    except NaverBlogPostError:
        raise
    except Exception as e:
        raise NaverBlogPostError(f"제목 입력 중 오류: {str(e)}")


async def fill_post_content(page: Page, content: str, use_html: bool = False) -> None:
    """블로그 글 본문을 한 줄씩 넣는다.

    스마트에디터 ONE의 본문은 iframe이 아니라 보이는
    `.se-component.se-text .se-text-paragraph` 이다. contenteditable 전체를
    잡으면 화면 밖 클립보드 헬퍼라서 클릭이 타임아웃되고, 그 경로로 편집기를
    비우려다 '본문 입력 전 편집기를 비우지 못했습니다'가 난다.
    """
    if use_html:
        logger.info("스마트에디터 ONE은 HTML 모드가 없어 텍스트로 입력합니다.")
    try:
        await ensure_continue_popup_cleared(page)
        selector = await click_editor_target(page, POST_WRITE_CONTENT_BODY, what="본문")
        await _sleep(0.3)
        # 줄은 있는 그대로 넣는다. URL 끝에 U+200B를 붙이면 스마트에디터가
        # 접두어(출처 예시:)를 지우고 주소를 다음 줄 끝에 붙인다.
        lines = content.split("\n")
        for index, line in enumerate(lines):
            if line:
                await page.keyboard.insert_text(line)
            if index < len(lines) - 1:
                await page.keyboard.press("Enter")
        logger.info("본문 입력 완료 (%s)", selector)
        await _sleep(0.5)
    except NaverBlogPostError:
        raise
    except PlaywrightTimeout as e:
        raise NaverBlogPostError(f"본문 입력 시간 초과: {str(e)}")
    except Exception as e:
        raise NaverBlogPostError(f"본문 입력 중 오류: {str(e)}")


async def save_draft(page: Page) -> Dict[str, Any]:
    """현재 작성 중인 글을 임시저장(저장)한다.

    발행하지 않고 에디터의 저장 버튼을 누른다. 클래스 해시
    `save_btn__...` 은 배포마다 바뀌므로 부분 일치로 찾는다.

    Returns:
        {"success": bool, "message": str, "post_url": None}
    """
    try:
        await page.bring_to_front()
        await _sleep(0.5)
        _frame, element, selector = await find_visible(
            page, NaverSelectors.POST_WRITE["temp_save_btn"]
        )
        if element is None:
            try:
                await page.screenshot(path="playwright-state/error_save_draft.png")
            except Exception:
                pass
            raise NaverBlogPostError("임시저장(저장) 버튼을 찾을 수 없습니다.")

        await element.click(timeout=5000)
        logger.info("임시저장 버튼 클릭: %s", selector)
        await _sleep(1)
        return {
            "success": True,
            "message": "임시저장되었습니다. (발행되지 않음)",
            "post_url": None,
        }
    except NaverBlogPostError:
        raise
    except Exception as e:
        raise NaverBlogPostError(f"임시저장 중 오류: {str(e)}")


async def _select_category(page: Page, category: str) -> bool:
    """발행 설정 레이어에서 카테고리를 선택한다(best-effort, 실패해도 발행 진행)."""
    for frame in page.frames:
        try:
            # 카테고리 셀렉트박스 열기
            openers = [
                "button[class*='selectbox_button']",
                "button:has-text('카테고리')",
                ".selectbox_button",
            ]
            for sel in openers:
                if await frame.locator(sel).count() > 0:
                    try:
                        await frame.locator(sel).first.click(timeout=3000)
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
                    break

            # 카테고리 항목 선택 (이름 매칭)
            item = frame.locator(
                f"label:has-text('{category}'):visible, "
                f"span:has-text('{category}'):visible, "
                f"a:has-text('{category}'):visible"
            )
            if await item.count() > 0:
                await item.first.click(timeout=3000)
                await asyncio.sleep(0.5)
                logger.info(f"카테고리 설정: {category}")
                return True
        except Exception:
            continue
    logger.warning(
        f"카테고리 '{category}' 설정 실패(요소 미발견). 기본 카테고리로 진행."
    )
    return False


async def _select_visibility(page: Page, visibility: str) -> bool:
    """발행 설정 레이어에서 공개범위를 선택한다(전체공개/이웃공개/서로이웃공개/비공개)."""
    # 정확 텍스트 매칭(:text-is)만 사용한다. 부분 매칭(:has-text)은 '비공개'가 포함된
    # 다른 요소를 잘못 클릭해 실제로는 공개범위가 안 바뀌었는데 성공으로 오인 →
    # 의도치 않은 전체공개 발행으로 이어질 수 있어 위험하다.
    for frame in page.frames:
        try:
            sels = [
                f"label:text-is('{visibility}'):visible",
                f"span:text-is('{visibility}'):visible",
                f"button:text-is('{visibility}'):visible",
            ]
            for sel in sels:
                loc = frame.locator(sel)
                if await loc.count() > 0:
                    await loc.first.click(timeout=3000)
                    await asyncio.sleep(0.5)
                    logger.info(f"공개범위 설정: {visibility}")
                    return True
        except Exception:
            continue
    logger.warning(f"공개범위 '{visibility}' 설정 실패(정확 매칭 요소 미발견).")
    return False


async def publish_post(
    page: Page,
    wait_for_completion: bool = True,
    timeout: int = 30000,
    category: Optional[str] = None,
    visibility: Optional[str] = None,
) -> Dict[str, Any]:
    """
    블로그 글을 발행합니다.

    Args:
        page: Playwright Page 객체
        wait_for_completion: 발행 완료를 기다릴지 여부
        timeout: 발행 완료 대기 시간 (ms)

    Returns:
        발행 결과 딕셔너리
        {
            "success": bool,
            "message": str,
            "post_url": str (발행된 글 URL, 성공 시)
        }

    Raises:
        NaverBlogPostError: 발행 실패 시
    """
    try:
        # 0. 메인 페이지로 포커스 전환 (iframe에서 나오기)
        # 명시적으로 메인 페이지로 전환
        await page.bring_to_front()
        await page.evaluate(
            "() => { if (window.parent) { window.parent.focus(); } window.focus(); }"
        )
        await asyncio.sleep(1)

        # 페이지가 실제로 로드되었는지 확인
        print(f"   현재 URL: {page.url}")
        print(f"   페이지 타이틀: {await page.title()}")

        # 페이지 내 모든 팝업/모달 닫기 (도움말 팝업 등)
        try:
            # 도움말 팝업 닫기
            popup_close_selectors = [
                "button.se-popup-button-cancel",  # 취소 버튼
                "button:has-text('닫기')",
                "button:has-text('확인')",
                "button.se-popup-close",
                ".se-popup-dim",  # 팝업 배경 클릭
            ]
            for close_sel in popup_close_selectors:
                popup_count = await page.locator(close_sel).count()
                if popup_count > 0:
                    try:
                        await page.locator(close_sel).first.click(timeout=2000)
                        print(f"   페이지 팝업 닫기: {close_sel}")
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass
        except Exception:
            pass

        # 1. 발행 버튼 찾기 (대체 셀렉터 시도)
        publish_clicked = False

        # 추가 발행 버튼 셀렉터
        # 네이버 블로그는 하단 중앙에 "글쓰기" 버튼이 있음 (이것이 발행 버튼)
        publish_selectors = [
            "div.publish_area button:has-text('글쓰기')",  # 하단 중앙 글쓰기 버튼
            "button.publish_btn",
            "a.publish_btn:has-text('글쓰기')",
            "button:has-text('글쓰기'):visible",
            "a:has-text('글쓰기'):visible",
            "button:has-text('발행'):visible",
            "button.se-toolbar-group-button.se-toolbar-publish-button",
            "button:has-text('등록')",
            "button.publish",
            "button.btn_post",
            "a:has-text('발행')",
            "a.btn_submit",
            "button[type='submit']",
        ]

        # 기존 셀렉터와 병합
        if isinstance(POST_WRITE_PUBLISH_BTN, list):
            publish_selectors = POST_WRITE_PUBLISH_BTN + publish_selectors
        else:
            publish_selectors.insert(0, POST_WRITE_PUBLISH_BTN)

        # 1. 모든 iframe에서 발행 버튼 찾기
        all_frames = page.frames
        for idx, frame in enumerate(all_frames):
            try:
                # Frame 내부의 도움말 팝업 닫기
                help_popup_selectors = [
                    "button.se-help-close-btn",
                    "button:has-text('닫기')",
                    ".se-help-close",
                ]
                for help_sel in help_popup_selectors:
                    help_count = await frame.locator(help_sel).count()
                    if help_count > 0:
                        await frame.locator(help_sel).first.click(timeout=2000)
                        await asyncio.sleep(0.5)
                        break

                # 발행 버튼 찾기 (우선순위: 발행 > 글쓰기)
                search_texts = ["발행", "글쓰기"]
                for search_text in search_texts:
                    write_btn_count = await frame.locator(
                        f"button:has-text('{search_text}'):visible"
                    ).count()
                    if write_btn_count > 0:
                        element = frame.locator(
                            f"button:has-text('{search_text}'):visible"
                        ).first
                        await element.click(timeout=5000)
                        publish_clicked = True
                        logger.info(f"발행 버튼 클릭 성공 (Frame {idx})")
                        await asyncio.sleep(2)
                        break

                if publish_clicked:
                    break
            except Exception:
                continue

        if not publish_clicked:
            await page.screenshot(path="playwright-state/error_publish_btn.png")
            raise NaverBlogPostError("발행 버튼을 찾을 수 없습니다.")

        # 2. 발행 설정(카테고리/공개범위) 적용
        if publish_clicked:
            await asyncio.sleep(1)  # 대화상자 로딩 대기

            # 카테고리: best-effort (실패 시 기본 카테고리로 진행)
            if category:
                await _select_category(page, category)

            # 공개범위: 비공개/이웃공개 등을 요청했는데 설정에 실패하면,
            # 의도치 않은 '전체공개' 발행을 막기 위해 발행을 중단한다.
            if visibility:
                vis_ok = await _select_visibility(page, visibility)
                if not vis_ok and visibility != "전체공개":
                    await page.screenshot(path="playwright-state/error_visibility.png")
                    raise NaverBlogPostError(
                        f"공개범위 '{visibility}' 설정에 실패하여 발행을 중단했습니다. "
                        "(의도치 않은 전체공개 방지 — 셀렉터 튜닝 필요)"
                    )

        # 3. 발행 설정 대화상자에서 최종 "발행" 버튼 클릭
        if publish_clicked:
            try:
                # 대화상자 내 발행 버튼을 force=True로 클릭 시도
                final_publish_clicked = False
                for idx, frame in enumerate(page.frames):
                    try:
                        dialog_publish_selectors = [
                            ".layer_popup__i0QOY button[class*='confirm']:has-text('발행')",
                            ".layer_popup__i0QOY button:has-text('발행')",
                        ]

                        for selector in dialog_publish_selectors:
                            try:
                                btn_count = await frame.locator(selector).count()
                                if btn_count > 0:
                                    await frame.locator(selector).first.click(
                                        force=True, timeout=5000
                                    )
                                    final_publish_clicked = True
                                    await asyncio.sleep(2)
                                    break
                            except Exception:
                                continue

                        if final_publish_clicked:
                            break
                    except Exception:
                        continue

                # JavaScript로 대화상자 내 발행 버튼 클릭 (fallback)
                if not final_publish_clicked:
                    for frame in page.frames:
                        try:
                            result = await frame.evaluate(
                                """
                                () => {
                                    const popup = document.querySelector('.layer_popup__i0QOY.is_show__TMSLq');
                                    if (!popup) return 'No popup';

                                    const buttons = popup.querySelectorAll('button');
                                    for (let btn of buttons) {
                                        if ((btn.textContent || '').trim() === '발행') {
                                            btn.click();
                                            return 'Clicked';
                                        }
                                    }
                                    return 'No button';
                                }
                            """
                            )
                            if "Clicked" in result:
                                await asyncio.sleep(3)
                                break
                        except Exception:
                            continue

            except Exception:
                pass

        # 3. 발행 완료 대기 (옵션)
        if wait_for_completion:
            try:
                # 발행 후 글 보기 페이지로 리다이렉트되는지 확인
                # URL 패턴: https://blog.naver.com/{blog_id}/{post_id}
                await page.wait_for_url("**/blog.naver.com/*/**", timeout=timeout)
                post_url = page.url

                # PostView 페이지인지 확인 (본문 영역이 있는지)
                # 글쓰기 페이지가 아닌 글 보기 페이지인지 체크
                if (
                    "postwrite" not in post_url.lower()
                    and "redirect=write" not in post_url.lower()
                ):
                    # URL이 {blog_id}/{post_id} 형태인지 확인
                    logger.info(f"발행 완료: {post_url}")
                    return {
                        "success": True,
                        "message": "글이 성공적으로 발행되었습니다.",
                        "post_url": post_url,
                    }
                else:
                    raise NaverBlogPostError("발행 후 페이지 이동에 실패했습니다.")

            except PlaywrightTimeout:
                raise NaverBlogPostError("발행 완료 대기 시간 초과")
        else:
            return {
                "success": True,
                "message": "발행 요청을 전송했습니다.",
                "post_url": None,
            }

    except Exception as e:
        raise NaverBlogPostError(f"발행 중 오류: {str(e)}")


async def create_blog_post(
    page: Page,
    title: str,
    content: str,
    blog_id: Optional[str] = None,
    use_html: bool = False,
    wait_for_completion: bool = True,
    category: Optional[str] = None,
    visibility: Optional[str] = None,
    draft: bool = False,
) -> Dict[str, Any]:
    """
    네이버 블로그에 새 글을 작성하고 발행하는 전체 프로세스.

    Args:
        page: Playwright Page 객체 (로그인된 상태여야 함)
        title: 글 제목
        content: 글 본문
        blog_id: 블로그 ID (옵션)
        use_html: HTML 모드로 본문 입력할지 여부
        wait_for_completion: 발행 완료를 기다릴지 여부

    Returns:
        발행 결과 딕셔너리
        {
            "success": bool,
            "message": str,
            "post_url": str,
            "title": str,
        }

    Raises:
        NaverBlogPostError: 글 작성 실패 시
    """
    try:
        # 1. 글쓰기 페이지로 이동
        await navigate_to_post_write_page(page, blog_id)

        # 2. 제목 입력
        await fill_post_title(page, title)

        # 3. 본문 입력
        await fill_post_content(page, content, use_html)

        # 4. 임시저장 또는 발행
        if draft:
            result = await save_draft(page)
        else:
            result = await publish_post(
                page,
                wait_for_completion,
                category=category,
                visibility=visibility,
            )

        result["title"] = title
        return result

    except NaverBlogPostError:
        raise
    except Exception as e:
        raise NaverBlogPostError(f"글 작성 중 오류: {str(e)}")
