"""네이버 로그인 자동화."""

import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from .selectors import LOGIN_ID_INPUT, LOGIN_PW_INPUT, LOGIN_BTN, NaverSelectors

logger = logging.getLogger(__name__)

# nid 로그인 직후 blog.naver.com 루트는 section 으로 리다이렉트되며 500 이 잦다.
# 로그인 완료 랜딩은 section.blog 로 고정한다.
LOGIN_URL = (
    "https://nid.naver.com/nidlogin.login?url=https%3A%2F%2Fsection.blog.naver.com%2F"
)
LOGIN_COOKIE_NAMES = frozenset({"NID_AUT", "NID_SES"})
# Either cookie is enough. NID_AUT is persistent and NID_SES is the session
# cookie; requiring both marked a signed-in browser as logged out.
NAVER_COOKIE_URLS = [
    "https://www.naver.com/",
    "https://naver.com/",
    "https://nid.naver.com/",
    "https://blog.naver.com/",
    "https://section.blog.naver.com/",
]
TWO_FACTOR_INPUTS = [
    "input[autocomplete='one-time-code']",
    "input[name='otp']",
    "#otp",
    "input[placeholder*='인증번호']",
]
TWO_FACTOR_TEXT = [
    "text=2단계 인증",
    "text=2-step verification",
    "text=2-Step Verification",
    "text=verification code",
    "text=Verification code",
]
LOGOUT_CONTROLS = [
    "a[href*='nidlogin.logout']",
    "button:has-text('로그아웃')",
    "a:has-text('로그아웃')",
    "button:has-text('Sign out')",
    "a:has-text('Logout')",
]
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


def _is_naver_auth_cookie(cookie: dict) -> bool:
    if cookie.get("name") not in LOGIN_COOKIE_NAMES:
        return False
    domain = str(cookie.get("domain") or "").lower()
    if "naver.com" in domain:
        return True
    url = str(cookie.get("url") or "").lower()
    return "naver.com" in url


async def _cookie_records(page: Page) -> list[dict]:
    """Read this context's cookies, including ones scoped to a Naver URL.

    cookies() with no URL can miss a host-only cookie that cookies(urls)
    still returns. Values stay in the caller and are never logged.
    """
    context = page.context
    records: list[dict] = []
    try:
        records.extend(await context.cookies())
    except Exception:
        logger.debug("context.cookies() failed", exc_info=True)
    try:
        scoped = await context.cookies(NAVER_COOKIE_URLS)
    except TypeError:
        scoped = []
    except Exception:
        logger.debug("scoped cookies() failed", exc_info=True)
        scoped = []
    records.extend(scoped or [])
    return records


async def auth_cookie_names(page: Page) -> set[str]:
    """Auth cookie names present on this MCP browser context. Never values."""
    names: set[str] = set()
    for cookie in await _cookie_records(page):
        if _is_naver_auth_cookie(cookie):
            names.add(str(cookie["name"]))
    return names


async def has_login_cookies(page: Page) -> bool:
    """True when NID_AUT or NID_SES belongs to this browser context."""
    return bool(await auth_cookie_names(page))


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


def _page_host(page: Page) -> str:
    try:
        return urllib.parse.urlparse(getattr(page, "url", "") or "").netloc.lower()
    except Exception:
        return ""


async def two_factor_visible(page: Page) -> bool:
    """OTP / 2-step UI on the Naver login host. Does not solve it."""
    for selector in TWO_FACTOR_INPUTS:
        if await _locator_visible(page, selector):
            return True
    if "nid.naver.com" not in _page_host(page):
        return False
    for selector in TWO_FACTOR_TEXT:
        if await _locator_visible(page, selector):
            return True
    return False


async def _logout_control_visible(page: Page) -> bool:
    for selector in LOGOUT_CONTROLS:
        if await _locator_visible(page, selector):
            return True
    return False


async def _switch_is_on(page: Page, selector: str) -> Optional[bool]:
    try:
        checked = await page.locator(selector).first.get_attribute("aria-checked")
    except Exception:
        return None
    if not isinstance(checked, str):
        return None
    lowered = checked.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


IP_SWITCH_INPUT = "#switchIP"
IP_SWITCH_TOGGLE = "label[for='switchIP']"


async def _ip_input_checked(page: Page) -> Optional[bool]:
    """현재 NID 스위치는 aria-checked가 갱신되지 않아 checked 속성으로 읽는다."""
    try:
        return await page.locator(IP_SWITCH_INPUT).first.is_checked()
    except Exception:
        return None


async def ensure_ip_security_off(page: Page) -> str:
    """Turn the login-form IP Security switch OFF before credentials.

    Returns already_off, turned_off, not_found, or still_on.
    Does not submit credentials. A missing or still-on switch must block submit.
    """
    # 현재 NID 마크업: 숨겨진 <input id="switchIP" role="switch">와
    # 이를 토글하는 <label for="switchIP">. input은 보이지 않으므로 존재만 본다.
    try:
        has_input = await page.locator(IP_SWITCH_INPUT).count() > 0
    except Exception:
        has_input = False
    if has_input:
        state = await _ip_input_checked(page)
        if state is None:
            return "not_found"
        if state is False:
            return "already_off"
        try:
            await page.locator(IP_SWITCH_TOGGLE).first.click()
        except Exception:
            logger.info("IP security switch could not be clicked")
            return "still_on"
        await _sleep(0.5)
        if await _ip_input_checked(page) is False:
            return "turned_off"
        return "still_on"

    selector = None
    for candidate in _selector_list(NaverSelectors.LOGIN["ip_security_switch"]):
        if await _locator_visible(page, candidate):
            selector = candidate
            break
    if selector is None:
        return "not_found"
    state = await _switch_is_on(page, selector)
    if state is None:
        return "not_found"
    if state is False:
        return "already_off"
    try:
        await page.locator(selector).click()
    except Exception:
        logger.info("IP security switch could not be clicked")
        return "still_on"
    if await _switch_is_on(page, selector) is False:
        return "turned_off"
    return "still_on"


async def _credentials_landed(page: Page, user_id: str) -> bool:
    try:
        current_id = await page.input_value(LOGIN_ID_INPUT)
        current_pw = await page.input_value(LOGIN_PW_INPUT)
    except Exception:
        return False
    return bool(current_id) and bool(current_pw) and current_id == user_id


def public_login_diagnostics(state: dict) -> dict:
    """Status safe to return to the MCP host. No cookie values or secrets."""
    parsed = urllib.parse.urlparse(state.get("page_url") or "")
    return {
        "status": state.get("status"),
        "logged_in": bool(state.get("logged_in")),
        "challenge": state.get("challenge"),
        "auth_cookie_names": list(state.get("auth_cookie_names") or []),
        "page_host": parsed.netloc,
        "page_path": parsed.path,
        "browser_owner": "mcp_playwright_context",
    }


async def inspect_login_state(page: Page) -> dict:
    """Describe this MCP page without navigating away from it.

    A challenge wins over stale auth cookies. A regular Chrome window is a
    different cookie jar and is not consulted.
    """
    try:
        names = sorted(await auth_cookie_names(page))
        if await captcha_visible(page):
            status, challenge, logged_in = "awaiting_captcha", "captcha", False
        elif await two_factor_visible(page):
            status, challenge, logged_in = "awaiting_2fa", "2fa", False
        elif names or await _logout_control_visible(page):
            status, challenge, logged_in = "authenticated", None, True
        elif await _locator_visible(page, LOGIN_ID_INPUT):
            status, challenge, logged_in = "login_form", None, False
        else:
            status, challenge, logged_in = "logged_out", None, False
        return {
            "logged_in": logged_in,
            "status": status,
            "challenge": challenge,
            "auth_cookie_names": names,
            "page_url": getattr(page, "url", "") or "",
        }
    except Exception:
        logger.warning("login state inspection failed", exc_info=True)
        return {
            "logged_in": False,
            "status": "unknown",
            "challenge": None,
            "auth_cookie_names": [],
            "page_url": getattr(page, "url", "") or "",
        }


async def _open_login_page(page: Page) -> None:
    # networkidle 은 nid 페이지에서 끝나지 않는다.
    # url= 파라미터가 붙은 로그인 페이지는 DOMContentLoaded가 끝나지 않을 때가 있다.
    # 응답 커밋만 기다리고 아이디 입력칸으로 준비 여부를 판단한다.
    await page.goto(LOGIN_URL, wait_until="commit", timeout=30000)
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
        await _type_secret(page, LOGIN_ID_INPUT, user_id)
    await _type_secret(page, LOGIN_PW_INPUT, password)
    if await _credentials_landed(page, user_id):
        return True
    try:
        if not await page.input_value(LOGIN_ID_INPUT):
            await page.locator(LOGIN_ID_INPUT).fill(user_id)
        if not await page.input_value(LOGIN_PW_INPUT):
            await page.locator(LOGIN_PW_INPUT).fill(password)
    except Exception:
        return False
    return await _credentials_landed(page, user_id)


async def _type_secret(page: Page, selector: str, text: str) -> None:
    """Type into a login field. The text is never logged."""
    await page.locator(selector).click()
    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
    except Exception:
        pass
    await page.keyboard.type(text, delay=80)


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

    if await two_factor_visible(page):
        logger.info("2단계 인증 화면입니다. 자동 클릭하지 않습니다.")
        return clicks

    refilled = await refill_credentials_if_cleared(page, user_id, password)
    if not refilled:
        return clicks

    if await captcha_visible(page) or await two_factor_visible(page):
        logger.info(
            "캡차 또는 2단계 인증이 보입니다. 정답은 사용자가 입력합니다. 자동 클릭하지 않습니다."
        )
        return clicks

    if not await _credentials_landed(page, user_id):
        logger.info("로그인 칸이 비어 있습니다. 로그인 버튼을 누르지 않습니다.")
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
            if headless and await two_factor_visible(page):
                raise NaverLoginError(
                    "2단계 인증이 필요합니다. HEADLESS=false에서 사용자가 직접 완료해야 합니다."
                )
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
        if headless and await two_factor_visible(page):
            raise NaverLoginError(
                "2단계 인증이 필요합니다. HEADLESS=false에서 사용자가 직접 완료해야 합니다."
            )
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


def cancel_login_assist() -> None:
    """Stop background credential refill so confirm cannot race a click."""
    for task in list(_assist_tasks):
        if not task.done():
            task.cancel()


def schedule_login_assist(
    page: Page,
    user_id: str,
    password: str,
    initial_clicks: int = 0,
) -> asyncio.Task:
    """MCP 도구가 반환된 뒤에도 캡차 후 비밀번호 재입력을 이어 간다."""
    cancel_login_assist()

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


def _login_outcome(status: str, *, submitted: bool, ip_security: str) -> dict:
    return {
        "status": status,
        "credentials_submitted": submitted,
        "ip_security": ip_security,
    }


async def start_login(page: Page, user_id: str, password: str) -> dict:
    """Start a deliberate login on this MCP page.

    IP Security is switched OFF before credentials are typed. A challenge,
    a missing button, or fields that stay empty is not reported as a submit.
    The page is not navigated away to blog home.

    Returns:
        status, credentials_submitted, ip_security. No secrets.
    """
    try:
        cancel_login_assist()
        await _open_login_page(page)
        ip_state = await ensure_ip_security_off(page)
        if ip_state not in {"already_off", "turned_off"}:
            logger.info(
                "IP security is not confirmed off (%s). Not submitting.", ip_state
            )
            return _login_outcome(
                "ip_security_not_confirmed_off",
                submitted=False,
                ip_security=ip_state,
            )

        if await two_factor_visible(page):
            return _login_outcome("awaiting_2fa", submitted=False, ip_security=ip_state)

        clicks = await assist_login_once(
            page,
            user_id,
            password,
            clicks=0,
            require_button=False,
        )
        submitted = clicks > 0

        if await two_factor_visible(page):
            if submitted:
                schedule_login_assist(page, user_id, password, initial_clicks=clicks)
            return _login_outcome(
                "awaiting_2fa", submitted=submitted, ip_security=ip_state
            )
        if await captcha_visible(page):
            schedule_login_assist(page, user_id, password, initial_clicks=clicks)
            return _login_outcome(
                "awaiting_captcha", submitted=submitted, ip_security=ip_state
            )
        if await has_login_cookies(page):
            return _login_outcome(
                "authenticated", submitted=submitted, ip_security=ip_state
            )
        if not await _credentials_landed(page, user_id):
            if not await _locator_visible(page, LOGIN_PW_INPUT):
                return _login_outcome(
                    "login_form_not_found", submitted=False, ip_security=ip_state
                )
            return _login_outcome(
                "credentials_not_entered", submitted=False, ip_security=ip_state
            )
        if clicks == 0:
            return _login_outcome(
                "login_button_not_found", submitted=False, ip_security=ip_state
            )

        schedule_login_assist(page, user_id, password, initial_clicks=clicks)
        logger.info(
            "Credentials submitted. Waiting for the user if a challenge appears."
        )
        return _login_outcome(
            "credentials_submitted", submitted=True, ip_security=ip_state
        )
    except NaverLoginError:
        raise
    except Exception as e:
        raise NaverLoginError(f"로그인 시작 중 오류 발생: {type(e).__name__}")


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
        ip_state = await ensure_ip_security_off(page)
        if ip_state not in {"already_off", "turned_off"}:
            raise NaverLoginError(
                "IP보안을 끄지 못해 로그인 정보를 제출하지 않았습니다."
            )
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

        logger.info("로그인 확인: NID_AUT 또는 NID_SES 쿠키")
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
    """True when this MCP context is authenticated.

    Does not navigate. Moving the login page to section.blog.naver.com
    showed a logged-out BlogHome and discarded an in-progress sign-in.
    A challenge is not authenticated even if a stale auth cookie exists.
    """
    state = await inspect_login_state(page)
    return bool(state["logged_in"])


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
