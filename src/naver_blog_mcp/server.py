"""네이버 블로그 MCP 서버.

이 모듈은 Claude가 네이버 블로그와 상호작용할 수 있도록
MCP (Model Context Protocol) 서버를 제공합니다.
"""

import asyncio
import logging
from typing import Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from .config import config, login_id
from .automation.browser import launch_browser, new_browser_context
from .services.session_manager import SessionManager, session_reuse_action
from .automation.login import inspect_login_state, verify_login_session
from .mcp.tools import (
    TOOLS_METADATA,
    handle_create_post,
    # handle_delete_post,  # 비활성화
    handle_list_categories,
    handle_list_posts,
    handle_read_post,
    handle_login,
    handle_confirm_login,
)
from .utils.trace_manager import trace_manager

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def page_is_open(page: object) -> bool:
    """True when the page has no closed flag or is still open."""
    is_closed = getattr(page, "is_closed", None)
    if not callable(is_closed):
        return True
    try:
        return not bool(is_closed())
    except Exception:
        return True


def preferred_page(pages, pinned):
    """Keep the deliberate login page when it is still open.

    Confirm and later tools must use that MCP page, not a probe page and
    not a separate Chrome window.
    """
    if pinned is not None and page_is_open(pinned):
        return pinned
    for page in pages:
        if page_is_open(page):
            return page
    return None


class NaverBlogMCPServer:
    """네이버 블로그 MCP 서버 클래스."""

    def __init__(self):
        """서버 초기화."""
        self.server = Server("naver-blog")
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self._login_page: Optional[Page] = None

        # 설정 검증
        config.validate()

        # 세션 관리자 초기화
        self.session_manager = SessionManager(
            user_id=login_id(),
            password=config.NAVER_BLOG_PASSWORD,
            storage_path=config.SESSION_STORAGE_PATH,
            session_validity_hours=config.SESSION_VALIDITY_HOURS,
        )

        # Tool 등록
        self._register_tools()

    def _register_tools(self):
        """MCP Tool들을 등록합니다."""
        logger.info("Registering MCP tools...")

        # naver_blog_create_post Tool 등록
        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[dict]:
            """Tool 호출 핸들러."""
            logger.info(f"Tool called: {name} with arguments: {arguments}")

            import json

            def _text(payload) -> list[dict]:
                if isinstance(payload, str):
                    return [{"type": "text", "text": payload}]
                return [
                    {
                        "type": "text",
                        "text": json.dumps(payload, ensure_ascii=False, indent=2),
                    }
                ]

            try:
                # 1) 로그인 시작: 기존 창을 닫고 새 headed 창에서 로그인 (2차 인증 수동)
                if name == "naver_blog_login":
                    await self.reset_browser_for_login()
                    page = await self.get_page()
                    self._login_page = page
                    result = await handle_login(
                        page,
                        login_id(),
                        config.NAVER_BLOG_PASSWORD,
                    )
                    return _text(result)

                # 그 외 도구는 브라우저만 보장 (대화형 로그인은 하지 않음)
                await self.ensure_browser()
                page = await self.get_page()

                # 2) 로그인 확인 및 세션 저장
                if name == "naver_blog_confirm_login":
                    result = await handle_confirm_login(
                        page, self.context, self.session_manager.storage_path
                    )
                    return _text(result)

                # 3) 콘텐츠 도구: 로그인 가드 (미로그인 시 안내만, 블로킹 금지)
                if not await verify_login_session(page):
                    return _text(
                        "네이버 로그인이 필요합니다. 먼저 naver_blog_login을 실행하고, "
                        "브라우저에서 2차 인증을 완료한 뒤 naver_blog_confirm_login으로 확인하세요."
                    )

                # Trace 시작
                if self.context:
                    await trace_manager.start_trace(self.context, name=name)

                # Tool별 핸들러 호출
                if name == "naver_blog_create_post":
                    result = await handle_create_post(
                        page=page,
                        title=arguments["title"],
                        content=arguments["content"],
                        category=arguments.get("category"),
                        tags=arguments.get("tags"),
                        images=arguments.get("images"),
                        publish=arguments.get("publish", False),
                        visibility=arguments.get("visibility", "비공개"),
                    )
                # elif name == "naver_blog_delete_post":
                #     result = await handle_delete_post(
                #         page=page, post_url=arguments["post_url"]
                #     )
                elif name == "naver_blog_list_categories":
                    result = await handle_list_categories(page=page)
                elif name == "naver_blog_list_posts":
                    result = await handle_list_posts(
                        page=page,
                        category_no=arguments.get("category_no"),
                    )
                elif name == "naver_blog_read_post":
                    result = await handle_read_post(
                        page=page,
                        log_no=arguments["log_no"],
                    )
                else:
                    return _text(f"알 수 없는 Tool: {name}")

                # Trace 저장 (성공)
                if self.context:
                    await trace_manager.stop_trace(self.context, success=True)

                return _text(result)

            except Exception as e:
                logger.error(f"Tool execution error: {e}", exc_info=True)

                # Trace 저장 (실패)
                if self.context:
                    try:
                        await trace_manager.stop_trace(self.context, success=False)
                    except Exception:
                        pass

                return _text(f"오류 발생: {str(e)}")

        # list_tools 핸들러 등록
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """사용 가능한 Tool 목록을 반환합니다."""
            # dict를 Tool 객체로 변환
            return [
                Tool(
                    name=tool_data["name"],
                    description=tool_data["description"],
                    inputSchema=tool_data["inputSchema"],
                )
                for tool_data in TOOLS_METADATA.values()
            ]

        logger.info(f"Registered {len(TOOLS_METADATA)} tools")

    async def ensure_browser(self):
        """브라우저/컨텍스트를 보장한다(lazy). 대화형 로그인은 하지 않는다.

        저장된 세션이 유효하면 재사용하고, 없으면 빈 컨텍스트(미로그인)를 만든다.
        실제 로그인은 naver_blog_login 도구에서만 수행한다.
        """
        if self.context:
            return

        if self.playwright is None:
            self.playwright = await async_playwright().start()

        if self.browser is None:
            self.browser = await launch_browser(self.playwright)
            logger.info("Browser launched (headless=%s)", config.HEADLESS)

        # 저장된 세션 파일이 있으면 이 브라우저 컨텍스트로만 복원한다.
        # 확인에 실패해도 파일은 지우지 않는다. 프로브가 실패하면 컨텍스트를 유지한다.
        if self.session_manager.is_session_file_valid():
            ctx = None
            probe = None
            state = {"logged_in": False, "status": "unknown"}
            try:
                ctx = await new_browser_context(
                    self.browser,
                    storage_state=self.session_manager.storage_path,
                )
                probe = await ctx.new_page()
                state = await inspect_login_state(probe)
            except Exception:
                logger.warning(
                    "세션 확인에 실패했습니다. 저장 파일은 유지하고 컨텍스트를 재사용합니다.",
                    exc_info=True,
                )
                state = {"logged_in": False, "status": "unknown"}
            if ctx is not None and session_reuse_action(state) == "reuse":
                if probe is not None:
                    try:
                        await probe.close()
                    except Exception:
                        pass
                self.context = ctx
                logger.info("저장된 세션 재사용")
                return
            if probe is not None:
                try:
                    await probe.close()
                except Exception:
                    pass
            if ctx is not None:
                try:
                    await ctx.close()
                except Exception:
                    pass
            logger.info("저장된 세션이 로그인 상태가 아닙니다. 세션 파일은 유지합니다.")

        # 빈 컨텍스트 (미로그인)
        self.context = await new_browser_context(self.browser)
        logger.info("빈 컨텍스트 생성 (미로그인)")

    async def reset_browser_for_login(self):
        """기존 창을 닫고 headed 새 브라우저/컨텍스트를 연다(로그인 준비).

        2차 인증 입력이 지연되어 기존 창이 stale 해진 경우에도, naver_blog_login을
        다시 호출하면 항상 깨끗한 새 로그인 창에서 시작하도록 한다.
        """
        # 저장 파일은 지우지 않는다. 이 호출은 메모리의 창만 새로 연다.
        self._login_page = None
        # 기존 컨텍스트/브라우저 정리
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.playwright is None:
            self.playwright = await async_playwright().start()

        # 2차 인증/CAPTCHA 수동 처리를 위해 headed 강제
        self.browser = await launch_browser(self.playwright, headless=False)
        self.context = await new_browser_context(self.browser)
        logger.info("로그인용 새 headed 브라우저/컨텍스트 생성")

    async def cleanup(self):
        """리소스 정리."""
        logger.info("Cleaning up resources...")

        if self.context:
            await self.context.close()
            logger.info("Browser context closed")

        if self.browser:
            await self.browser.close()
            logger.info("Browser closed")

        if self.playwright:
            await self.playwright.stop()
            logger.info("Playwright stopped")

    async def get_page(self) -> Page:
        """새 페이지를 생성하거나 기존 페이지를 반환합니다.

        Returns:
            Playwright Page 객체

        """
        # 브라우저/컨텍스트 보장 (lazy)
        await self.ensure_browser()

        pages = list(self.context.pages) if self.context is not None else []
        chosen = preferred_page(pages, self._login_page)
        if chosen is not None:
            return chosen
        return await self.context.new_page()

    async def run(self):
        """MCP 서버 실행."""
        try:
            # 브라우저는 lazy 초기화 (첫 도구 호출 시 ensure_browser).
            # startup에서 로그인/브라우저를 띄우면 MCP 핸드셰이크가 막혀 연결 실패하므로 제거.

            # stdio를 통해 MCP 서버 실행
            async with stdio_server() as (read_stream, write_stream):
                logger.info("MCP Server started successfully (lazy browser init)")
                await self.server.run(
                    read_stream,
                    write_stream,
                    self.server.create_initialization_options(),
                )
        except Exception as e:
            logger.error(f"Server error: {e}", exc_info=True)
            raise
        finally:
            # 리소스 정리
            await self.cleanup()


async def async_main():
    """비동기 서버 엔트리포인트."""
    server = NaverBlogMCPServer()
    await server.run()


def main():
    """동기 서버 엔트리포인트 (CLI 진입점)."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
