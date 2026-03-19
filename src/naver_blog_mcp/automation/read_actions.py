"""네이버 블로그 글 읽기 관련 자동화 기능."""

import logging
import re
from typing import Dict, Any, List, Optional
from playwright.async_api import Page

from ..config import config
from ..utils.exceptions import NaverBlogError
from ..utils.error_handler import handle_playwright_error

logger = logging.getLogger(__name__)


async def _get_blog_id(page: Page, blog_id: Optional[str] = None) -> str:
    """blog_id를 결정합니다."""
    if blog_id:
        return blog_id

    current_url = page.url
    if "blog.naver.com" in current_url:
        match = re.search(r'blogId=([^&]+)', current_url)
        if match:
            return match.group(1)
        match = re.search(r'blog\.naver\.com/([^/?]+)', current_url)
        if match:
            extracted_id = match.group(1)
            if extracted_id not in ["PostList", "MyBlog", "PostView"]:
                return extracted_id

    return config.NAVER_BLOG_ID


async def _access_main_frame(page: Page):
    """iframe#mainFrame에 접근합니다."""
    iframe_element = await page.wait_for_selector(
        "iframe#mainFrame", timeout=15000
    )
    main_frame = await iframe_element.content_frame()
    if not main_frame:
        raise NaverBlogError("mainFrame iframe에 접근할 수 없습니다")
    return main_frame


async def list_posts(
    page: Page,
    blog_id: Optional[str] = None,
    category_no: Optional[str] = None,
) -> Dict[str, Any]:
    """네이버 블로그의 글 목록을 조회합니다.

    Args:
        page: Playwright Page 객체
        blog_id: 블로그 아이디 (None이면 현재 로그인한 블로그)
        category_no: 카테고리 번호 (None이면 전체)

    Returns:
        {
            "success": bool,
            "message": str,
            "posts": [
                {
                    "title": str,
                    "logNo": str,
                    "date": str,
                    "categoryName": str,
                    "categoryNo": str,
                    "post_url": str,
                },
                ...
            ]
        }
    """
    try:
        logger.info("글 목록 조회 시작")
        bid = await _get_blog_id(page, blog_id)
        all_posts = []
        current_page = 1

        while True:
            # URL 구성 - 리스트 뷰 (categoryNo=0이면 전체)
            cat_no = category_no or "0"
            url = (
                f"https://blog.naver.com/PostList.naver"
                f"?blogId={bid}"
                f"&categoryNo={cat_no}"
                f"&currentPage={current_page}"
                f"&from=postList"
                f"&parentCategoryNo="
            )

            await page.goto(url, wait_until="networkidle")
            logger.info(f"페이지 {current_page} 접근: {url}")

            main_frame = await _access_main_frame(page)

            # 글 목록 테이블에서 제목 링크 추출
            # 리스트 뷰: table#listTopForm 안의 행들
            post_rows = await main_frame.query_selector_all(
                "table.blog2_list tr"
            )

            if not post_rows:
                # 대체 셀렉터: 리스트 형태가 아닌 경우
                post_rows = await main_frame.query_selector_all(
                    "#postListBody tr, .blog2_series tr"
                )

            page_posts = []

            for row in post_rows:
                try:
                    # 제목 링크 찾기
                    title_link = await row.query_selector(
                        "a.pcol2, a[href*='PostView']"
                    )
                    if not title_link:
                        continue

                    title = await title_link.text_content()
                    href = await title_link.get_attribute("href")

                    if not title or not href:
                        continue

                    title = title.strip()

                    # logNo 추출
                    log_no_match = re.search(r'logNo=(\d+)', href)
                    if not log_no_match:
                        continue
                    log_no = log_no_match.group(1)

                    # 날짜 추출
                    date_el = await row.query_selector("td.date, span.date")
                    date_text = ""
                    if date_el:
                        date_text = (await date_el.text_content() or "").strip()

                    # 카테고리 추출
                    cat_el = await row.query_selector(
                        "td.cate a, a[href*='categoryNo=']"
                    )
                    cat_name = ""
                    cat_no_val = ""
                    if cat_el:
                        cat_name = (await cat_el.text_content() or "").strip()
                        cat_href = await cat_el.get_attribute("href") or ""
                        cat_match = re.search(r'categoryNo=(\d+)', cat_href)
                        if cat_match:
                            cat_no_val = cat_match.group(1)

                    post_url = f"https://blog.naver.com/{bid}/{log_no}"

                    page_posts.append({
                        "title": title,
                        "logNo": log_no,
                        "date": date_text,
                        "categoryName": cat_name,
                        "categoryNo": cat_no_val,
                        "post_url": post_url,
                    })

                except Exception as e:
                    logger.warning(f"글 정보 추출 중 오류: {e}")
                    continue

            if not page_posts:
                # 테이블이 아닌 썸네일/카드 뷰에서 추출 시도
                post_links = await main_frame.query_selector_all(
                    "a[href*='PostView.naver'], a[href*='/PostView']"
                )
                seen_log_nos = set()
                for link in post_links:
                    try:
                        href = await link.get_attribute("href") or ""
                        log_no_match = re.search(r'logNo=(\d+)', href)
                        if not log_no_match:
                            # /blogId/logNo 패턴
                            log_no_match = re.search(rf'{bid}/(\d+)', href)
                        if not log_no_match:
                            continue

                        log_no = log_no_match.group(1)
                        if log_no in seen_log_nos:
                            continue
                        seen_log_nos.add(log_no)

                        title = (await link.text_content() or "").strip()
                        if not title or len(title) < 2:
                            continue

                        page_posts.append({
                            "title": title,
                            "logNo": log_no,
                            "date": "",
                            "categoryName": "",
                            "categoryNo": cat_no if cat_no != "0" else "",
                            "post_url": f"https://blog.naver.com/{bid}/{log_no}",
                        })
                    except Exception as e:
                        logger.warning(f"대체 추출 중 오류: {e}")
                        continue

            if not page_posts:
                logger.info(f"페이지 {current_page}: 글이 없으므로 종료")
                break

            all_posts.extend(page_posts)
            logger.info(f"페이지 {current_page}: {len(page_posts)}개 글 발견")

            # 다음 페이지 확인
            next_link = await main_frame.query_selector(
                "a.next, a:has-text('다음')"
            )
            if not next_link:
                break

            current_page += 1

            # 봇 감지 방지를 위한 짧은 대기
            await page.wait_for_timeout(1000)

        logger.info(f"총 {len(all_posts)}개 글 조회 완료")
        return {
            "success": True,
            "message": f"{len(all_posts)}개의 글을 찾았습니다",
            "posts": all_posts,
        }

    except Exception as e:
        custom_error = await handle_playwright_error(e, page, "list_posts")
        logger.error(f"글 목록 조회 실패: {custom_error}", exc_info=True)
        return {
            "success": False,
            "message": f"글 목록 조회 실패: {str(custom_error)}",
            "posts": [],
        }


async def read_post(
    page: Page,
    log_no: str,
    blog_id: Optional[str] = None,
) -> Dict[str, Any]:
    """네이버 블로그 개별 글의 본문을 읽습니다.

    Args:
        page: Playwright Page 객체
        log_no: 글 번호
        blog_id: 블로그 아이디 (None이면 현재 로그인한 블로그)

    Returns:
        {
            "success": bool,
            "message": str,
            "title": str,
            "date": str,
            "categoryName": str,
            "content_html": str,
            "content_text": str,
            "tags": list[str],
            "images": list[str],
        }
    """
    try:
        bid = await _get_blog_id(page, blog_id)
        post_url = f"https://blog.naver.com/{bid}/{log_no}"
        logger.info(f"글 읽기 시작: {post_url}")

        await page.goto(post_url, wait_until="networkidle")

        main_frame = await _access_main_frame(page)

        # 제목 추출
        title = ""
        title_selectors = [
            ".se-module-text .se-text-paragraph",
            ".se-title-text",
            ".pcol1",
            "#title_1",
            "h3.se_textarea",
        ]
        for sel in title_selectors:
            el = await main_frame.query_selector(sel)
            if el:
                title = (await el.text_content() or "").strip()
                if title:
                    break

        # 제목을 못 찾으면 og:title 메타 태그에서 시도
        if not title:
            try:
                title = await page.evaluate(
                    "document.querySelector('meta[property=\"og:title\"]')?.content || ''"
                )
                title = title.strip()
            except Exception:
                pass

        # 날짜 추출
        date_text = ""
        date_selectors = [
            "span.se_publishDate",
            ".blog_date",
            "p.date",
            ".se-date",
            "span.date",
        ]
        for sel in date_selectors:
            el = await main_frame.query_selector(sel)
            if el:
                date_text = (await el.text_content() or "").strip()
                if date_text:
                    # "2024. 1. 15. 10:30" 같은 형식 정규화
                    date_text = date_text.replace("\n", " ").strip()
                    break

        # 카테고리 추출
        category_name = ""
        cat_selectors = [
            "a[href*='categoryNo=']",
            ".blog2_category a",
            ".cate a",
        ]
        for sel in cat_selectors:
            els = await main_frame.query_selector_all(sel)
            for el in els:
                text = (await el.text_content() or "").strip()
                href = await el.get_attribute("href") or ""
                # "전체보기" 등 제외
                if text and text not in ["전체보기", ""] and "categoryNo=" in href:
                    cat_match = re.search(r'categoryNo=(\d+)', href)
                    if cat_match and cat_match.group(1) != "0":
                        category_name = text
                        break
            if category_name:
                break

        # 본문 HTML 추출
        content_html = ""
        content_text = ""
        content_selectors = [
            "div.se-main-container",
            "#postViewArea",
            ".post-view",
            "#post-view",
        ]
        for sel in content_selectors:
            el = await main_frame.query_selector(sel)
            if el:
                content_html = await el.inner_html()
                content_text = (await el.text_content() or "").strip()
                if content_html:
                    break

        # 이미지 URL 추출
        images = []
        if content_html:
            img_els = await main_frame.query_selector_all(
                "div.se-main-container img, #postViewArea img"
            )
            for img_el in img_els:
                src = await img_el.get_attribute("src")
                data_lazy = await img_el.get_attribute("data-lazy-src")
                img_url = data_lazy or src
                if img_url and "postfiles.pstatic.net" in img_url:
                    # 원본 크기 이미지 URL로 변환
                    img_url = re.sub(r'\?type=.*$', '', img_url)
                    if img_url not in images:
                        images.append(img_url)

        # 태그 추출
        tags = []
        tag_selectors = [
            ".post_tag a",
            "a.tag",
            ".wrap_tag a",
            "div.post_tag_label a",
        ]
        for sel in tag_selectors:
            tag_els = await main_frame.query_selector_all(sel)
            for tag_el in tag_els:
                tag_text = (await tag_el.text_content() or "").strip()
                if tag_text and tag_text.startswith("#"):
                    tag_text = tag_text[1:]  # '#' 제거
                if tag_text and tag_text not in tags:
                    tags.append(tag_text)
            if tags:
                break

        logger.info(
            f"글 읽기 완료: title='{title}', date='{date_text}', "
            f"images={len(images)}, tags={len(tags)}"
        )

        return {
            "success": True,
            "message": "글을 성공적으로 읽었습니다",
            "title": title,
            "date": date_text,
            "categoryName": category_name,
            "content_html": content_html,
            "content_text": content_text,
            "tags": tags,
            "images": images,
        }

    except Exception as e:
        custom_error = await handle_playwright_error(e, page, "read_post")
        logger.error(f"글 읽기 실패: {custom_error}", exc_info=True)
        return {
            "success": False,
            "message": f"글 읽기 실패: {str(custom_error)}",
            "title": "",
            "date": "",
            "categoryName": "",
            "content_html": "",
            "content_text": "",
            "tags": [],
            "images": [],
        }
