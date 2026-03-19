"""네이버 블로그 DOM 셀렉터 정의.

네이버 UI 변경에 대응하기 위해 대체 셀렉터를 리스트로 관리합니다.
"""

from typing import List, Union

# 타입 정의
Selector = Union[str, List[str]]


class NaverSelectors:
    """네이버 블로그 셀렉터 클래스."""

    # 로그인 페이지
    LOGIN = {
        "id_input": "#id",
        "pw_input": "#pw",
        "login_btn": [".btn_login", "button[type='submit']"],
        "error_message": ".error_message",
    }

    # 블로그 메인
    BLOG_MAIN = {
        "profile": [".my_nick", ".profile_info"],
        "write_btn": ["a[href*='PostWriteForm']", ".write_btn"],
    }

    # 글쓰기 페이지
    POST_WRITE = {
        "title_input": [
            "div[contenteditable='true'][data-placeholder='제목']",  # 스마트에디터 ONE
            "div[contenteditable='true']:has-text('제목')",
            "input[placeholder*='제목']",
            "#title",
            ".se-title-input",
        ],
        "content_frame": ["iframe.se-iframe", "iframe#mainFrame"],
        "content_body": [
            "div[contenteditable='true']",  # 일반 contenteditable
            ".se-component-content",
            ".se-text-paragraph",
        ],
        "category_select": [".blog2_series", "select[name='category']"],
        "tag_input": ["input[placeholder*='태그']", ".tag_input"],
        "publish_btn": [
            "button:has-text('발행')",
            ".publish_btn",
            "button[type='submit']",
        ],
        "temp_save_btn": ["button:has-text('임시저장')", ".temp_save_btn"],
        "image_upload_btn": [
            "button[aria-label='사진']",
            ".image_upload",
            "button:has-text('사진')",
        ],
    }

    # 글 보기 페이지
    POST_VIEW = {
        "post_url_pattern": "**/PostView.naver*",
        "edit_btn": ["a:has-text('수정')", ".edit_btn"],
        "delete_btn": ["a:has-text('삭제')", ".delete_btn"],
    }

    # 글 목록 페이지
    POST_LIST = {
        "post_title_link": [
            "a.pcol2",  # 일반 리스트 뷰
            "a[href*='PostView']",
            ".title a",
            ".post-title a",
        ],
        "post_date": [
            "td.date",  # 리스트 뷰 날짜 칼럼
            "span.date",
            ".post-date",
        ],
        "pagination_next": [
            "a.next",
            "a:has-text('다음')",
        ],
        "category_item": [
            "a[href*='categoryNo=']",
        ],
    }

    # 글 읽기 (본문 추출)
    POST_READ = {
        "post_title": [
            ".se-module-text .se-text-paragraph",  # SmartEditor ONE
            ".se-title-text",
            "div.se-component.se-sticker .se-module-text",
            ".pcol1",
            "#title_1",
        ],
        "post_content": [
            "div.se-main-container",  # SmartEditor ONE
            "#postViewArea",  # 구 에디터
            ".post-view",
            "#post-view",
        ],
        "post_date": [
            "span.se_publishDate",
            ".blog_date",
            "p.date",
            ".se-date",
        ],
        "post_tags": [
            ".post_tag a",
            "a.tag",
            ".wrap_tag a",
        ],
    }

    @classmethod
    def get_selector(cls, category: str, key: str) -> Selector:
        """
        카테고리와 키로 셀렉터 가져오기.

        Args:
            category: 셀렉터 카테고리 (LOGIN, BLOG_MAIN, POST_WRITE, POST_VIEW)
            key: 셀렉터 키

        Returns:
            셀렉터 문자열 또는 대체 셀렉터 리스트

        Raises:
            KeyError: 존재하지 않는 카테고리나 키
        """
        category_dict = getattr(cls, category, None)
        if category_dict is None:
            raise KeyError(f"존재하지 않는 카테고리: {category}")

        selector = category_dict.get(key)
        if selector is None:
            raise KeyError(f"존재하지 않는 셀렉터 키: {key}")

        return selector


# 편의를 위한 상수
LOGIN_ID_INPUT = NaverSelectors.LOGIN["id_input"]
LOGIN_PW_INPUT = NaverSelectors.LOGIN["pw_input"]
LOGIN_BTN = NaverSelectors.LOGIN["login_btn"]

# 글쓰기 관련 상수
POST_WRITE_TITLE = NaverSelectors.POST_WRITE["title_input"]
POST_WRITE_CONTENT_FRAME = NaverSelectors.POST_WRITE["content_frame"]
POST_WRITE_CONTENT_BODY = NaverSelectors.POST_WRITE["content_body"]
POST_WRITE_PUBLISH_BTN = NaverSelectors.POST_WRITE["publish_btn"]
POST_WRITE_CATEGORY_BTN = NaverSelectors.POST_WRITE["category_select"]
POST_WRITE_TAG_INPUT = NaverSelectors.POST_WRITE["tag_input"]
