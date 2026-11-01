"""네이버 로그인 자동화."""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .selectors import LOGIN_ID_INPUT, LOGIN_PW_INPUT, LOGIN_BTN, NaverSelectors

logger = logging.getLogger(__name__)

# nid 로그인 직후 blog.naver.com 루트는 section 으로 리다이렉트되며 500 이 잦다.
# 로그인 완료 랜딩은 section.blog 로 고정한다.
LOGIN_URL = (
    "https://nid.naver.com/nidlogin.login"
    "?url=https%3A%2F%2Fsection.blog.naver.com%2F"
)
SECTION_BLOG_URL = "https://section.blog.naver.com/"
LOGIN_COOKIE_NAMES = frozenset({"NID_AUT", "NID_SES"})
MAX_LOGIN_AUTO_CLICKS = 3
# 헤드풀에서는 사람이 캡차를 풀 시간을 둔다 (약 10분).
LOGIN_ASSIST_POLLS = 300
LOGIN_ASSIST_INTERVAL_SECONDS = 2.0

_assist_tasks: set[asyncio.Task] = set()


async def _sleep(seconds: float) -> None:
    """테스트가 전역 asyncio.sleep 을 건드리지 않고 대기만 건너뛰게 한다."""
    await asyncio.sleep(seconds)


class NaverLoginError(Exception):
    """네이버 로그인 관련 에러."""

    pass


class CaptchaDetectedError(NaverLoginError):
    """CAPTCHA 감지됨."""

    pass


class InvalidCredentialsError(NaverLoginError):
    """잘못된 로그인 정보."""

    pass


def _selector_list(selectors: object) -> list[str]:
    if isinstance(selectors, str):
        return [selectors]
    return list(selectors)  # type: ignore[arg-type]


async def _naver_cookie_names(page: Page) -> set[str]:
    cookies = await page.context.cookies()
    return {
        cookie["name"] for cookie in cookies if "naver.com" in cookie.get("domain", "")
    }


async def has_login_cookies(page: Page) -> bool:
    """NID_AUT 와 NID_SES 가 있으면 로그인된 세션이다."""
    return LOGIN_COOKIE_NAMES <= await _naver_cookie_names(page)


async def _locator_visible(page: Page, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        return await locator.count() > 0 and await locator.first.is_visible()
    except Exception:
        return False


async def captcha_visible(page: Page) -> bool:
    """영수증 캡차 등 추가 확인 화면이 보이면 True."""
    for selector in _selector_list(NaverSelectors.LOGIN["captcha"]):
        if await _locator_visible(page, selector):
            return True
    return False


async def _open_login_page(page: Page) -> None:
    # networkidle 은 nid 페이지에서 끝나지 않는다.
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_selector(LOGIN_ID_INPUT, timeout=15000)
    await _sleep(1)


async def click_login_button(page: Page) -> bool:
    """현재 NID 마크업의 로그인 버튼을 누른다. 없으면 False."""
    for selector in _selector_list(LOGIN_BTN):
        try:
            await page.click(selector, timeout=3000)
            return True
        except PlaywrightTimeout:
            continue
        except Exception:
            continue
    return False


async def refill_credentials_if_cleared(
    page: Page, user_id: str, password: str
) -> bool:
    """비밀번호 칸이 보이고 비어 있을 때만 아이디/비밀번호를 다시 입력한다.

    캡차 이후 로그인 폼으로 돌아오면 비밀번호가 지워진다.
    이미 값이 있으면 사용자가 입력 중인 것으로 보고 건드리지 않는다.
    page.fill 은 네이버 로그인 입력의 실제 값으로 반영되지 않는 경우가 있어
    클릭 후 키보드 입력을 사용한다.

    Returns:
        이번에 비밀번호를 채웠으면 True.
    """
    if not await _locator_visible(page, LOGIN_PW_INPUT):
        return False
    try:
        if await page.input_value(LOGIN_PW_INPUT):
            return False
    except Exception:
        return False

    try:
        current_id = await page.input_value(LOGIN_ID_INPUT)
    except Exception:
        current_id = ""
    if not current_id:
        await page.locator(LOGIN_ID_INPUT).click()
        await page.keyboard.type(user_id, delay=80)
    await page.locator(LOGIN_PW_INPUT).click()
    await page.keyboard.type(password, delay=80)
    return True


async def assist_login_once(
    page: Page,
    user_id: str,
    password: str,
    clicks: int,
    max_auto_clicks: int = MAX_LOGIN_AUTO_CLICKS,
    require_button: bool = False,
) -> int:
    """로그인 폼을 한 번 보조한다.

    비밀번호가 비어 있으면 다시 채운다. 캡차가 보이면 정답은 사람에게 맡기고
    로그인 버튼을 누르지 않는다. 자동 클릭은 max_auto_clicks 회로 제한한다.
    """
    if await has_login_cookies(page):
        return clicks

    refilled = await refill_credentials_if_cleared(page, user_id, password)
    if not refilled:
        return clicks

    if await captcha_visible(page):
        logger.info(
            "캡차가 보입니다. 정답은 사용자가 입력합니다. 자동 클릭하지 않습니다."
        )
        return clicks

    if clicks >= max_auto_clicks:
        logger.info(
            "로그인 자동 클릭 %s회를 넘겼습니다. 사용자가 직접 로그인 버튼을 눌러 주세요.",
            max_auto_clicks,
        )
        return clicks

    await _sleep(0.8)
    if await click_login_button(page):
        logger.info("로그인 버튼 자동 클릭 (%s)", clicks + 1)
        return clicks + 1

    if require_button:
        raise NaverLoginError("로그인 버튼을 찾을 수 없습니다.")
    return clicks


async def run_login_assist(
    page: Page,
    user_id: str,
    password: str,
    initial_clicks: int = 0,
    max_polls: int = LOGIN_ASSIST_POLLS,
    interval: float = LOGIN_ASSIST_INTERVAL_SECONDS,
    max_auto_clicks: int = MAX_LOGIN_AUTO_CLICKS,
    headless: bool = False,
) -> bool:
    """쿠키가 생길 때까지 로그인 폼을 다시 채운다.

    headless=True 이고 캡차가 보이면 CaptchaDetectedError.
    헤드풀에서는 캡차 답을 기다렸다가, 폼이 비면 비밀번호만 다시 넣는다.
    """
    clicks = initial_clicks
    try:
        for _ in range(max_polls):
            if await has_login_cookies(page):
                return True
            if headless and await captcha_visible(page):
                raise CaptchaDetectedError(
                    "CAPTCHA가 감지되었습니다. HEADLESS=false로 설정하고 수동으로 풀어주세요."
                )
            clicks = await assist_login_once(
                page,
                user_id,
                password,
                clicks,
                max_auto_clicks=max_auto_clicks,
            )
            if await has_login_cookies(page):
                return True
            await _sleep(interval)
        if headless and await captcha_visible(page):
            raise CaptchaDetectedError(
                "CAPTCHA가 감지되었습니다. HEADLESS=false로 설정하고 수동으로 풀어주세요."
            )
        return await has_login_cookies(page)
    except (CaptchaDetectedError, InvalidCredentialsError, NaverLoginError):
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("로그인 보조 루프가 중단되었습니다.", exc_info=True)
        return False


def schedule_login_assist(
    page: Page,
    user_id: str,
    password: str,
    initial_clicks: int = 0,
) -> asyncio.Task:
    """MCP 도구가 반환된 뒤에도 캡차 후 비밀번호 재입력을 이어 간다."""
    for task in list(_assist_tasks):
        if not task.done():
            task.cancel()

    task = asyncio.create_task(
        run_login_assist(
            page,
            user_id,
            password,
            initial_clicks=initial_clicks,
        ),
        name="naver-login-assist",
    )
    _assist_tasks.add(task)
    task.add_done_callback(_assist_tasks.discard)
    return task


async def _visible_error_message(page: Page) -> Optional[str]:
    try:
        locator = page.locator(".error_message:visible")
        if await locator.count() == 0:
            return None
        text = await locator.first.text_content()
    except Exception:
        return None
    if text and text.strip():
        return text.strip()
    return None


async def start_login(page: Page, user_id: str, password: str) -> None:
    """네이버 로그인 페이지에서 ID/PW 제출을 시작한다.

    호출은 바로 반환된다. 영수증 캡차 때문에 비밀번호가 지워지면 백그라운드
    루프가 몇 번까지 다시 채우고, 캡차 답과 그 이후의 클릭은 사용자가 한다.
    완료 여부는 verify_login_session 또는 naver_blog_confirm_login 으로 확인한다.

    Raises:
        NaverLoginError: 로그인 페이지/버튼 처리 실패 시
    """
    try:
        await _open_login_page(page)
        clicks = await assist_login_once(
            page,
            user_id,
            password,
            clicks=0,
            require_button=True,
        )
        if (
            clicks == 0
            and not await has_login_cookies(page)
            and not await captcha_visible(page)
            and not await _locator_visible(page, LOGIN_PW_INPUT)
        ):
            raise NaverLoginError("로그인 폼을 찾지 못했습니다.")

        schedule_login_assist(page, user_id, password, initial_clicks=clicks)
        logger.info("로그인 정보 제출 완료. 2차 인증/CAPTCHA는 사용자 확인 대기.")
    except NaverLoginError:
        raise
    except Exception as e:
        raise NaverLoginError(f"로그인 시작 중 오류 발생: {str(e)}")


async def login_to_naver(
    page: Page,
    user_id: str,
    password: str,
    storage_state_path: Optional[str] = None,
    headless: bool = True,
) -> dict:
    """
    네이버에 로그인하고 세션을 저장합니다.

    로그인 성공은 blog.naver.com 이동이 아니라 NID_AUT/NID_SES 쿠키로 판단한다.
    blog.naver.com 은 section.blog.naver.com 으로 리다이렉트되며 500 을 내는
    경우가 있어, 페이지 로딩에만 의존하면 로그인된 세션을 놓친다.

    Args:
        page: Playwright Page 객체
        user_id: 네이버 아이디
        password: 네이버 비밀번호
        storage_state_path: 세션 저장 경로 (기본: playwright-state/auth.json)
        headless: 헤드리스 모드 여부

    Returns:
        로그인 결과 딕셔너리
        {
            "success": bool,
            "message": str,
            "storage_state_path": str (세션 저장 경로)
        }

    Raises:
        CaptchaDetectedError: CAPTCHA가 감지된 경우
        InvalidCredentialsError: 로그인 정보가 잘못된 경우
        NaverLoginError: 기타 로그인 에러
    """
    if storage_state_path is None:
        storage_state_path = "playwright-state/auth.json"

    try:
        await _open_login_page(page)
        logged_in = await run_login_assist(
            page,
            user_id,
            password,
            max_polls=8 if headless else LOGIN_ASSIST_POLLS,
            interval=1.0 if headless else LOGIN_ASSIST_INTERVAL_SECONDS,
            headless=headless,
        )
        if not logged_in:
            error_message = await _visible_error_message(page)
            if error_message:
                raise InvalidCredentialsError(f"로그인 실패: {error_message}")
            raise NaverLoginError("로그인에 실패했습니다.")

        Path(storage_state_path).parent.mkdir(parents=True, exist_ok=True)
        await page.context.storage_state(path=storage_state_path)

        if not await has_login_cookies(page):
            raise NaverLoginError("로그인 후 세션 확인에 실패했습니다. (쿠키 없음)")

        logger.info("로그인 확인: NID_AUT/NID_SES 쿠키")
        return {
            "success": True,
            "message": "로그인에 성공했습니다.",
            "storage_state_path": storage_state_path,
        }

    except CaptchaDetectedError:
        raise
    except InvalidCredentialsError:
        raise
    except NaverLoginError:
        raise
    except PlaywrightTimeout as e:
        raise NaverLoginError(f"로그인 시간 초과: {str(e)}")
    except Exception as e:
        raise NaverLoginError(f"로그인 중 오류 발생: {str(e)}")


async def verify_login_session(page: Page) -> bool:
    """
    현재 세션이 로그인 상태인지 확인합니다.

    NID_AUT 와 NID_SES 가 있으면 blog.naver.com 으로 이동하지 않는다.
    그 페이지는 500 을 내는 경우가 있어, 이동 결과에만 의존하면
    로그인된 세션을 미로그인으로 오판한다.
    """
    try:
        if await has_login_cookies(page):
            return True

        await page.goto(
            SECTION_BLOG_URL,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        await _sleep(1)

        if "nid.naver.com" in page.url:
            return False

        if await has_login_cookies(page):
            return True

        # section.blog.naver.com 도 호스트에 blog.naver.com 이 포함된다.
        if "blog.naver.com" in page.url:
            cookies = await page.context.cookies()
            return any(cookie["name"] == "NID_AUT" for cookie in cookies)

        return False
    except Exception:
        return False


async def logout_from_naver(page: Page) -> None:
    """
    네이버에서 로그아웃합니다.

    Args:
        page: Playwright Page 객체
    """
    try:
        await page.goto("https://nid.naver.com/nidlogin.logout")
        await _sleep(1)
    except Exception as e:
        raise NaverLoginError(f"로그아웃 중 오류 발생: {str(e)}")
