"""MCP Tool 정의.

이 모듈은 Claude가 호출할 수 있는 네이버 블로그 관련 Tool들을 정의합니다.
"""

import logging
from typing import Optional, Dict, Any

from playwright.async_api import Page

from pathlib import Path

from ..automation.post_actions import (
    create_blog_post,
    finalize_current_post,
    NaverBlogPostError,
)
from ..automation.image_upload import upload_images
from ..automation.category_actions import get_categories
from ..automation.read_actions import list_posts, read_post
from ..automation.login import (
    cancel_login_assist,
    inspect_login_state,
    public_login_diagnostics,
    start_login,
)
from ..config import login_id, publish_blog_id
from ..utils.retry import retry_on_error
from ..utils.error_handler import handle_playwright_error
from ..utils.exceptions import NaverBlogError, UploadError

logger = logging.getLogger(__name__)

TOOLS_METADATA = {
    "naver_blog_login": {
        "name": "naver_blog_login",
        "description": (
            "이 MCP 브라우저에서 네이버 로그인을 시작합니다. "
            "IP보안이 꺼진 뒤에만 설정된 로그인 아이디와 비밀번호를 제출합니다. "
            "캡차나 2단계 인증은 사용자가 그 브라우저에서 직접 완료합니다. "
            "credentials_submitted가 true가 아니면 자격 증명을 제출한 것이 아닙니다. "
            "완료 후 naver_blog_confirm_login으로 이 브라우저의 세션만 확인하세요."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "naver_blog_confirm_login": {
        "name": "naver_blog_confirm_login",
        "description": (
            "이 MCP 브라우저 컨텍스트의 로그인만 확인합니다. 다른 Chrome 창은 보지 않습니다. "
            "NID_AUT 또는 NID_SES가 있고 캡차/2단계 인증 화면이 아니면 세션 파일을 저장합니다. "
            "실패해도 기존 세션 파일은 지우지 않고, 로그인 페이지를 블로그 홈으로 옮기지 않습니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "naver_blog_finalize_post": {
        "name": "naver_blog_finalize_post",
        "description": (
            "이미 열려 있는 글쓰기 편집기만 발행합니다. 새 글쓰기 페이지로 이동하지 않습니다. "
            "편집기 제목이 title과 정확히 같고, visibility 공개범위를 선택했을 때만 최종 발행을 누릅니다. "
            "success가 false이면 발행이 확인되지 않은 것이며, 글이 없다고 단정하지 않습니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "편집기에 이미 표시된 제목과 정확히 같아야 합니다.",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["전체공개", "이웃공개", "서로이웃공개", "비공개"],
                    "description": "발행 공개범위. 선택에 실패하면 최종 발행을 누르지 않습니다.",
                },
                "category": {
                    "type": "string",
                    "description": "선택할 카테고리. 지정했는데 찾지 못하면 발행하지 않습니다.",
                },
            },
            "required": ["title", "visibility"],
        },
    },
    "naver_blog_create_post": {
        "name": "naver_blog_create_post",
        "description": "네이버 블로그에 새 글을 작성합니다. 이미지 첨부도 지원합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "글 제목",
                },
                "content": {
                    "type": "string",
                    "description": "글 본문 내용",
                },
                "category": {
                    "type": "string",
                    "description": "카테고리 이름 (선택)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "태그 목록 (선택)",
                },
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "첨부할 이미지 파일 경로 목록 (선택). 본문 작성 전에 이미지를 먼저 업로드합니다.",
                },
                "publish": {
                    "type": "boolean",
                    "description": "발행 여부. false면 임시저장(발행되지 않아 남에게 안 보임), true면 발행. 안전을 위해 기본값은 false(임시저장).",
                    "default": False,
                },
                "visibility": {
                    "type": "string",
                    "enum": ["전체공개", "이웃공개", "서로이웃공개", "비공개"],
                    "description": "발행 시 공개범위(publish=true일 때만 적용). 기본값은 비공개. 전체공개로 올리려면 명시적으로 '전체공개'를 지정해야 함.",
                    "default": "비공개",
                },
            },
            "required": ["title", "content"],
        },
    },
    # NOTE: 글 삭제 기능은 일단 비활성화 (필요시 추후 구현)
    # "naver_blog_delete_post": {
    #     "name": "naver_blog_delete_post",
    #     "description": "네이버 블로그의 글을 삭제합니다.",
    #     "inputSchema": {
    #         "type": "object",
    #         "properties": {
    #             "post_url": {
    #                 "type": "string",
    #                 "description": "삭제할 글의 URL",
    #             },
    #         },
    #         "required": ["post_url"],
    #     },
    # },
    "naver_blog_list_categories": {
        "name": "naver_blog_list_categories",
        "description": "네이버 블로그의 카테고리 목록을 가져옵니다.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    "naver_blog_list_posts": {
        "name": "naver_blog_list_posts",
        "description": "네이버 블로그의 글 목록을 조회합니다. 카테고리별 필터링이 가능합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category_no": {
                    "type": "string",
                    "description": "카테고리 번호 (선택, 미지정시 전체 글 조회)",
                },
            },
            "required": [],
        },
    },
    "naver_blog_read_post": {
        "name": "naver_blog_read_post",
        "description": "네이버 블로그의 개별 글 본문을 읽습니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "log_no": {
                    "type": "string",
                    "description": "글 번호 (logNo)",
                },
            },
            "required": ["log_no"],
        },
    },
}


def get_tools_list() -> list[dict]:
    """등록된 Tool 목록을 반환합니다.

    Returns:
        Tool 메타데이터 리스트
    """
    return list(TOOLS_METADATA.values())


# ============================================================================
# Tool Handler Functions
# ============================================================================


async def handle_login(page: Page, user_id: str, password: str) -> Dict[str, Any]:
    """네이버 로그인을 시작합니다(ID/PW 제출까지, 논블로킹).

    2차 인증/CAPTCHA는 사용자가 헤드풀 브라우저에서 직접 완료해야 하며,
    완료 후 handle_confirm_login으로 확인한다.

    Args:
        page: Playwright Page (headed, 새로 연 로그인 창)
        user_id: 네이버 아이디
        password: 네이버 비밀번호

    Returns:
        작업 결과 딕셔너리
    """
    logger.info("네이버 로그인 시작")
    try:
        outcome = await start_login(page, user_id, password)
    except Exception as e:
        logger.error("로그인 시작 실패: %s", type(e).__name__, exc_info=True)
        return {
            "success": False,
            "status": "login_error",
            "credentials_submitted": False,
            "message": f"로그인 시작 실패: {type(e).__name__}",
        }

    status = str(outcome.get("status") or "login_error")
    submitted = bool(outcome.get("credentials_submitted"))
    payload = {
        "status": status,
        "credentials_submitted": submitted,
        "ip_security": outcome.get("ip_security"),
    }
    messages = {
        "credentials_submitted": (
            "아이디/비밀번호를 제출했습니다. "
            "2단계 인증이나 캡차가 보이면 이 브라우저에서 직접 완료해 주세요. "
            "완료 후 naver_blog_confirm_login으로 확인합니다."
        ),
        "awaiting_2fa": (
            "2단계 인증 화면입니다. 사용자가 이 MCP 브라우저에서 인증을 마쳐야 합니다. "
            "로그인 완료가 아닙니다."
        ),
        "awaiting_captcha": (
            "캡차 화면입니다. 정답은 사용자가 입력합니다. 자동으로 통과시키지 않습니다."
        ),
        "credentials_not_entered": (
            "로그인 칸에 값이 들어가지 않았습니다. 로그인 버튼을 누르지 않았습니다."
        ),
        "login_button_not_found": (
            "로그인 버튼을 찾지 못했습니다. 한국어와 영어 화면의 버튼을 확인하세요."
        ),
        "ip_security_not_confirmed_off": (
            "IP보안이 꺼진 것을 확인하지 못해 로그인 정보를 제출하지 않았습니다."
        ),
        "login_form_not_found": "로그인 폼을 찾지 못했습니다.",
        "authenticated": (
            "이 MCP 브라우저에 로그인 쿠키가 있습니다. "
            "naver_blog_confirm_login으로 세션을 저장하세요."
        ),
    }
    payload["success"] = status in {
        "credentials_submitted",
        "authenticated",
    } or (submitted and status in {"awaiting_2fa", "awaiting_captcha"})
    payload["message"] = messages.get(status, "로그인을 시작하지 못했습니다.")
    return payload


async def handle_confirm_login(
    page: Page, context, storage_path: str
) -> Dict[str, Any]:
    """로그인(2차 인증 포함) 완료 여부를 확인하고, 성공 시 세션을 저장합니다.

    Args:
        page: Playwright Page
        context: Playwright BrowserContext (세션 저장 대상)
        storage_path: 세션 저장 경로

    Returns:
        작업 결과 딕셔너리
    """
    logger.info("로그인 확인 시작")
    cancel_login_assist()
    try:
        state = await inspect_login_state(page)
        diagnostics = public_login_diagnostics(state)
        diagnostics["configured_login_id"] = login_id()
        diagnostics["configured_publish_blog_id"] = publish_blog_id()
        if not state["logged_in"]:
            return {
                "success": False,
                "logged_in": False,
                "status": state["status"],
                "message": _confirm_message(state),
                "diagnostics": diagnostics,
            }

        Path(storage_path).parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=storage_path)
        logger.info("세션 저장 완료: %s", storage_path)
        return {
            "success": True,
            "logged_in": True,
            "status": "authenticated",
            "message": (
                "이 MCP 브라우저에서 로그인을 확인했습니다. 세션 파일을 저장했습니다. "
                f"발행 블로그 설정은 {publish_blog_id() or '(없음)'} 입니다."
            ),
            "diagnostics": diagnostics,
            "configured_login_id": login_id(),
            "configured_publish_blog_id": publish_blog_id(),
        }
    except Exception:
        logger.error("로그인 확인 중 오류", exc_info=True)
        return {
            "success": False,
            "logged_in": False,
            "status": "unknown",
            "message": "로그인 확인 중 오류가 발생했습니다. 저장된 세션 파일은 유지했습니다.",
        }


def _confirm_message(state: dict) -> str:
    status = state.get("status")
    if status == "awaiting_2fa":
        return (
            "2단계 인증 화면입니다. 이 MCP 브라우저에서 사용자가 인증을 마칠 때까지 대기합니다. "
            "로그인 성공으로 처리하지 않았고, 페이지는 이동하지 않았습니다."
        )
    if status == "awaiting_captcha":
        return (
            "보안 확인(캡차) 화면입니다. 정답은 사용자가 입력합니다. "
            "로그인 성공으로 처리하지 않았고, 페이지는 이동하지 않았습니다."
        )
    return (
        "이 MCP 브라우저 컨텍스트에는 네이버 로그인 쿠키가 없습니다. "
        "다른 Chrome 창의 로그인과는 별개의 세션입니다. 페이지는 이동하지 않았고, "
        "저장된 세션 파일은 지우지 않았습니다."
    )


@retry_on_error
async def handle_create_post(
    page: Page,
    title: str,
    content: str,
    category: Optional[str] = None,
    tags: Optional[list[str]] = None,
    images: Optional[list[str]] = None,
    publish: bool = False,
    visibility: Optional[str] = "비공개",
) -> Dict[str, Any]:
    """네이버 블로그에 새 글을 작성합니다.

    Args:
        page: Playwright Page 객체 (로그인된 상태)
        title: 글 제목
        content: 글 본문 내용
        category: 카테고리 이름 (선택)
        tags: 태그 목록 (선택)
        images: 첨부할 이미지 파일 경로 목록 (선택)
        publish: 즉시 발행 여부 (기본: True, False면 임시저장)

    Returns:
        작업 결과 딕셔너리
        {
            "success": bool,
            "message": str,
            "post_url": str (발행 시),
            "title": str,
            "images_uploaded": int (업로드된 이미지 수)
        }

    Raises:
        NaverBlogPostError: 글 작성 실패 시
        UploadError: 이미지 업로드 실패 시
    """
    try:
        logger.info(f"글 작성 시작: {title}")
        images_uploaded = 0

        # 1. 이미지 업로드 (본문 작성 전)
        if images:
            logger.info(f"이미지 업로드 시작: {len(images)}개")
            try:
                upload_result = await upload_images(page, images)
                images_uploaded = len(upload_result.get("uploaded", []))

                if upload_result.get("failed"):
                    logger.warning(
                        f"일부 이미지 업로드 실패: {upload_result['failed']}"
                    )

                logger.info(f"이미지 업로드 완료: {images_uploaded}/{len(images)}개")

            except UploadError as e:
                logger.error(f"이미지 업로드 실패: {e}")
                return {
                    "success": False,
                    "message": f"이미지 업로드 실패: {str(e)}",
                    "post_url": None,
                    "title": title,
                    "images_uploaded": 0,
                }

        # 2. 본문 작성 (publish=false면 임시저장, true면 지정 공개범위로 발행)
        result = await create_blog_post(
            page=page,
            title=title,
            content=content,
            blog_id=None,  # 현재 로그인된 블로그 사용
            use_html=False,
            wait_for_completion=True,
            category=category,
            visibility=visibility,
            draft=(not publish),
        )

        # 결과에 이미지 정보 추가
        result["images_uploaded"] = images_uploaded

        logger.info(
            f"글 작성 완료: {result.get('post_url', 'N/A')} (이미지 {images_uploaded}개)"
        )
        return result

    except NaverBlogPostError as e:
        logger.error(f"글 작성 실패: {e}")
        return {
            "success": False,
            "message": f"글 작성 중 오류가 발생했습니다: {str(e)}",
            "post_url": None,
            "title": title,
            "images_uploaded": images_uploaded,
        }
    except Exception as e:
        # Playwright 에러를 커스텀 에러로 변환
        custom_error = await handle_playwright_error(e, page, "create_post")
        logger.error(f"예상치 못한 오류: {custom_error}", exc_info=True)

        # 재시도 가능한 에러면 다시 발생시켜서 tenacity가 재시도하도록
        if isinstance(custom_error, NaverBlogError):
            raise custom_error

        return {
            "success": False,
            "message": f"예상치 못한 오류: {str(custom_error)}",
            "post_url": None,
            "title": title,
        }


# NOTE: 글 삭제 기능은 일단 비활성화 (필요시 추후 구현)
# async def handle_delete_post(page: Page, post_url: str) -> Dict[str, Any]:
#     """네이버 블로그의 글을 삭제합니다.
#
#     Args:
#         page: Playwright Page 객체 (로그인된 상태)
#         post_url: 삭제할 글의 URL
#
#     Returns:
#         작업 결과 딕셔너리
#         {
#             "success": bool,
#             "message": str,
#             "post_url": str
#         }
#     """
#     # TODO: 필요시 추후 구현
#     logger.warning("handle_delete_post: 아직 구현되지 않았습니다.")
#     return {
#         "success": False,
#         "message": "글 삭제 기능은 아직 구현되지 않았습니다.",
#         "post_url": post_url,
#     }


async def handle_list_posts(
    page: Page,
    category_no: Optional[str] = None,
) -> Dict[str, Any]:
    """네이버 블로그의 글 목록을 조회합니다.

    Args:
        page: Playwright Page 객체
        category_no: 카테고리 번호 (선택)

    Returns:
        작업 결과 딕셔너리
    """
    logger.info(f"글 목록 조회 시작 (categoryNo={category_no})")

    try:
        result = await list_posts(page, category_no=category_no)

        if result["success"]:
            logger.info(f"글 목록 조회 완료: {len(result['posts'])}개")
        else:
            logger.error(f"글 목록 조회 실패: {result['message']}")

        return result

    except Exception as e:
        logger.error(f"글 목록 조회 중 예외 발생: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"글 목록 조회 실패: {str(e)}",
            "posts": [],
        }


async def handle_read_post(
    page: Page,
    log_no: str,
) -> Dict[str, Any]:
    """네이버 블로그의 개별 글 본문을 읽습니다.

    Args:
        page: Playwright Page 객체
        log_no: 글 번호

    Returns:
        작업 결과 딕셔너리
    """
    logger.info(f"글 읽기 시작: logNo={log_no}")

    try:
        result = await read_post(page, log_no=log_no)

        if result["success"]:
            logger.info(f"글 읽기 완료: {result['title']}")
        else:
            logger.error(f"글 읽기 실패: {result['message']}")

        return result

    except Exception as e:
        logger.error(f"글 읽기 중 예외 발생: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"글 읽기 실패: {str(e)}",
            "title": "",
            "date": "",
            "categoryName": "",
            "content_html": "",
            "content_text": "",
            "tags": [],
            "images": [],
        }


async def handle_finalize_post(
    page: Page,
    title: str,
    visibility: str,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish the already-open editor. Does not log the title or body."""
    logger.info("열린 편집기 발행 확인")
    try:
        return await finalize_current_post(
            page,
            expected_title=title,
            visibility=visibility,
            category=category,
        )
    except Exception:
        logger.error("편집기 발행 확인 중 오류", exc_info=True)
        return {
            "success": False,
            "status": "publish_unconfirmed",
            "message": "발행 확인 중 오류가 발생했습니다. 발행 여부는 확인되지 않았습니다.",
            "post_url": None,
        }


async def handle_list_categories(page: Page) -> Dict[str, Any]:
    """네이버 블로그의 카테고리 목록을 가져옵니다.

    Args:
        page: Playwright Page 객체 (로그인된 상태)

    Returns:
        작업 결과 딕셔너리
        {
            "success": bool,
            "message": str,
            "categories": [
                {
                    "name": str,
                    "url": str,
                    "categoryNo": str
                },
                ...
            ]
        }
    """
    logger.info("카테고리 목록 조회 시작")

    try:
        result = await get_categories(page)

        if result["success"]:
            logger.info(f"카테고리 조회 완료: {len(result['categories'])}개")
        else:
            logger.error(f"카테고리 조회 실패: {result['message']}")

        return result

    except Exception as e:
        logger.error(f"카테고리 조회 중 예외 발생: {e}", exc_info=True)
        return {
            "success": False,
            "message": f"카테고리 조회 실패: {str(e)}",
            "categories": [],
        }
