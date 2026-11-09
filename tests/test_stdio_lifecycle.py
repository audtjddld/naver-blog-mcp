"""Stdio lifecycle with the maintained MCP SDK client.

The host reported that a tool call died during live discovery while the
server process stayed in epoll. This drives the real entrypoint the same
way mcp.client.stdio does: initialize, list, call, list again.
"""

import os
import sys
from datetime import timedelta

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "naver_blog_login",
    "naver_blog_confirm_login",
    "naver_blog_finalize_post",
    "naver_blog_create_post",
    "naver_blog_list_categories",
    "naver_blog_list_posts",
    "naver_blog_read_post",
}


def _server_env(tmp_path) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "/workspace/src"
    env["NAVER_BLOG_ID"] = "stdio-probe"
    env["NAVER_BLOG_PASSWORD"] = "stdio-probe-secret"
    env["NAVER_PUBLISH_BLOG_ID"] = "stdio-probe-blog"
    env["HEADLESS"] = "true"
    env["SESSION_STORAGE_PATH"] = str(tmp_path / "no-such-session.json")
    env.pop("NAVER_LOGIN_ID", None)
    return env


@pytest.mark.asyncio
async def test_sdk_client_lists_twice_and_calls_without_dropping_session(tmp_path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from naver_blog_mcp.server import main; main()"],
        env=_server_env(tmp_path),
        cwd="/workspace",
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=20),
        ) as session:
            initialized = await session.initialize()
            assert initialized.capabilities.tools is not None

            first = await session.list_tools()
            assert {tool.name for tool in first.tools} == EXPECTED_TOOLS

            second = await session.list_tools()
            assert {tool.name for tool in second.tools} == EXPECTED_TOOLS

            # Browser launch is expected to fail in this dev env. The session
            # must still answer the call and accept another list.
            result = await session.call_tool(
                "naver_blog_confirm_login",
                {},
                read_timeout_seconds=timedelta(seconds=20),
            )
            assert result.content
            assert "stdio-probe-secret" not in "".join(
                getattr(block, "text", "") for block in result.content
            )

            third = await session.list_tools()
            assert {tool.name for tool in third.tools} == EXPECTED_TOOLS
