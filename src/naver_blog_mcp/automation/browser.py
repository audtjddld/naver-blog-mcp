"""브라우저 실행과 컨텍스트 생성.

자동화 표식(navigator.webdriver, --enable-automation)을 줄인다.
네이버 로그인 캡차가 이 표식에 더 자주 뜨는 것이 확인되었다.
"""

from typing import Any

from ..config import get_browser_config, get_context_config

STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


def browser_launch_kwargs(**overrides: Any) -> dict:
    """chromium.launch 에 넘길 인자. overrides 가 기본값을 덮어쓴다."""
    options = get_browser_config()
    options.update(overrides)
    return options


async def launch_browser(playwright: Any, **overrides: Any) -> Any:
    """자동화 표식을 끈 Chromium 을 실행한다."""
    return await playwright.chromium.launch(**browser_launch_kwargs(**overrides))


async def new_browser_context(browser: Any, **overrides: Any) -> Any:
    """한국어 로캘의 컨텍스트를 만들고 webdriver 플래그를 숨긴다.

    storage_state 등 None 값은 Playwright 에 넘기지 않는다.
    """
    options = get_context_config()
    options.update(
        {key: value for key, value in overrides.items() if value is not None}
    )
    context = await browser.new_context(**options)
    await context.add_init_script(STEALTH_INIT_SCRIPT)
    return context
