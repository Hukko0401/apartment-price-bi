"""Crawl tin đăng batdongsan.com.vn theo từng dự án -> Supabase.

Bước 1  list   : mỗi dự án trong crawl.project_sources -> mở trang dự án, tìm link
                 "Tin mua bán tại <dự án>" -> duyệt các trang danh sách -> crawl.listing_urls
Bước 2  detail : mỗi tin chưa xong trong crawl.listing_urls -> crawl.listings

Chạy từ THƯ MỤC GỐC của repo:
    python -m crawlers.batdongsan_listings list --limit 2        # thử bước 1 với 2 dự án
    python -m crawlers.batdongsan_listings detail --limit 5      # thử bước 2 với 5 tin
    python -m crawlers.batdongsan_listings                       # chạy cả 2 bước

Cần mở sẵn Chrome ở chế độ debug (xem core/browser.py).
Log ghi vào logs/batdongsan_listings_YYYYMMDD.log
"""
from __future__ import annotations

import argparse
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from core.browser import connect_chrome, looks_blocked
from core.db import Db
from core.errors import BlockedError, ListingGone
from core.logger import setup_logging
from core.parse import (clean_text, el_text, parse_area_m2, parse_date_vn, parse_int,
                        parse_price_vnd)
from core.util import sleep_between, worker_name

SOURCE_NAME = "batdongsan"
PARSER_VERSION = "1.0.0"  # Sửa logic bóc dữ liệu thì đổi số này (vd 1.0.0 -> 1.1.0)

BASE_URL = "https://batdongsan.com.vn"
WAIT_TIMEOUT = 20
PAGE_SIZE_GUESS = 20              # số tin tối đa/trang list, dùng để đoán "còn trang sau không"
MAX_CONSECUTIVE_FAILS = 5         # lỗi liên tiếp quá số này thì dừng (nghi bị chặn)
APARTMENT_PREFIXES = ("ban-can-ho-chung-cu",)  # URL tin thuộc nhóm căn hộ chung cư

LIST_PAGE_DELAY = (3.0, 5.0)      # nghỉ giữa các trang list
DETAIL_DELAY = (1.5, 3.0)         # nghỉ giữa các tin
PROJECT_DELAY = (5.0, 12.0)       # nghỉ giữa các dự án

log = logging.getLogger("batdongsan.listings")

# =============================================================== hàm tiện ích / parse

def clean_url(url: str) -> str:
    parsed = urlparse(urljoin(BASE_URL, url))
    return parsed._replace(query="", fragment="").geturl()


def extract_listing_id(url: str) -> Optional[str]:
    m = re.search(r"pr(\d+)(?:[/?#.]|$)", url)
    return m.group(1) if m else None


def category_of(url: str) -> Optional[str]:
    parts = urlparse(url).path.strip("/").split("/")
    return parts[0] if parts and parts[0] else None


def build_page_url(list_url: str, page: int) -> str:
    return list_url if page == 1 else f"{list_url.rstrip('/')}/p{page}"


# =============================================================== bước 1: danh sách tin
def parse_cards(soup: BeautifulSoup, project_id: str, page_number: int,
                page_url: str) -> Tuple[List[Dict[str, Any]], int]:
    """Bóc các card tin đăng. Trả về (danh sách card hợp lệ, số card thô trên trang)."""
    nodes = soup.select("#product-lists-web .re__card-full")
    if not nodes:
        nodes = soup.select(".re__card-full")
        if nodes:
            log.warning("   Không thấy #product-lists-web, dùng .re__card-full (%s card)", len(nodes))

    cards: List[Dict[str, Any]] = []
    for node in nodes:
        classes = " ".join(node.get("class") or [])
        if "promoted" in classes:
            continue
        if node.select_one(".re__native-sponsored-ad") or node.select_one("img[src*='expired']"):
            continue
        link = node.select_one("a.js__product-link-for-product-id")
        if not link or not link.get("href"):
            continue
        url = clean_url(link["href"])
        if "batdongsan.com.vn" not in url:
            continue
        listing_id = link.get("data-product-id") or extract_listing_id(url)
        if not listing_id:
            continue

        inner = node.select_one(".js__card") or node  # thuộc tính vtp nằm ở .js__card
        area = el_text(node.select_one(".re__card-config-area"))
        published = node.select_one(".re__card-published-info-published-at")
        category = category_of(url)
        cards.append({
            "source_name": SOURCE_NAME,
            "source_listing_id": str(listing_id),
            "listing_url": url,
            "project_id": project_id,
            "category_slug": category,
            "is_apartment": bool(category and category.startswith(APARTMENT_PREFIXES)),
            "list_page_url": page_url,
            "page_number": page_number,
            "vip_type": inner.get("vtp") or node.get("vtp"),
            "card_title": el_text(node.select_one(".js__card-title")) or clean_text(link.get("title")),
            "card_price_raw": el_text(node.select_one(".re__card-config-price")),
            "card_area_raw": area.lstrip("·").strip() if area else None,
            "card_location_raw": el_text(node.select_one(".re__card-location span")),
            "card_posted_raw": (published.get("aria-label") if published else None),
        })
    return cards, len(nodes)


def has_next_page(soup: BeautifulSoup, page: int) -> bool:
    nxt = soup.select_one("a.re__pagination-next")
    if nxt is not None:
        return "disabled" not in " ".join(nxt.get("class") or [])
    return soup.select_one(f'a[href$="/p{page + 1}"]') is not None


def find_listing_list_url(driver, project_url: str) -> Optional[str]:
    """Mở trang dự án, lấy link 'Tin mua bán tại <dự án>'. None nếu dự án không có mục này."""
    driver.get(project_url)
    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".project-main-info"))
        )
    except TimeoutException:
        if looks_blocked(driver):
            raise BlockedError("Trang dự án bị chặn")
        raise
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 2);")  # kích hoạt phần lazy
    try:
        WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.js__list-product-area-sale h2 a"))
        )
    except TimeoutException:
        return None
    href = driver.find_element(By.CSS_SELECTOR, "div.js__list-product-area-sale h2 a").get_attribute("href")
    return clean_url(href) if href else None


def load_list_page(driver, url: str) -> BeautifulSoup:
    driver.get(url)
    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#product-lists-web"))
        )
    except TimeoutException:
        if looks_blocked(driver):
            raise BlockedError(f"Trang list bị chặn: {url}")
        # không bị chặn: có thể trang thật sự rỗng, để parse_cards trả 0 card
    driver.execute_script("window.scrollBy(0, 600);")
    sleep_between((1.0, 2.0))
    return BeautifulSoup(driver.page_source, "html.parser")


def crawl_project(driver, db, project: Dict[str, Any], max_pages: int,
                  all_types: bool) -> Tuple[int, int, Optional[str]]:
    """Trả về (số tin tìm thấy, số tin mới, link list)."""
    project_id = project["project_id"]
    list_url = project.get("listing_list_url")
    if not list_url:
        list_url = find_listing_list_url(driver, project["url"])
        if not list_url:
            log.info("   Dự án không có mục tin đăng -> 0 tin")
            return 0, 0, None
        log.info("   Link tin đăng: %s", list_url)
        sleep_between(LIST_PAGE_DELAY)

    seen: set = set()
    new_total = 0
    for page in range(1, max_pages + 1):
        page_url = build_page_url(list_url, page)
        soup = load_list_page(driver, page_url)
        cards, raw_count = parse_cards(soup, project_id, page, page_url)
        fresh = [c for c in cards if c["source_listing_id"] not in seen]
        log.info("   Trang %s: %s card, %s hợp lệ, %s mới trong lần chạy này",
                 page, raw_count, len(cards), len(fresh))
        if not fresh:
            if page == 1:
                log.warning("   Trang 1 không có tin hợp lệ (%s)", page_url)
            break
        seen.update(c["source_listing_id"] for c in fresh)
        new_total += db.save_listing_urls(fresh, all_types)
        if not has_next_page(soup, page) and raw_count < PAGE_SIZE_GUESS:
            break
        sleep_between(LIST_PAGE_DELAY)
    else:
        log.warning("   Chạm giới hạn --max-pages=%s, có thể còn tin chưa lấy", max_pages)
    return len(seen), new_total, list_url


def run_list(driver, db, limit: Optional[int] = None, max_pages: int = 50,
             max_attempts: int = 3, all_types: bool = False,
             project_id: Optional[str] = None, stale_minutes: int = 60) -> None:
    log.info("=" * 60)
    log.info("BƯỚC 1: CRAWL DANH SÁCH TIN THEO DỰ ÁN -> crawl.listing_urls")
    log.info("=" * 60)

    reset = db.release_stale_jobs(SOURCE_NAME, stale_minutes)
    if reset:
        log.info("Đã đưa %s dự án 'running' bị kẹt về 'pending'", reset)

    worker = worker_name()
    if project_id:
        limit = 1  # chỉ xử lý đúng 1 dự án được chỉ định
    ok = failed = consecutive_fails = 0

    while limit is None or (ok + failed) < limit:
        project = db.claim_source_job(SOURCE_NAME, worker, max_attempts, project_id)
        if not project:
            log.info("Không còn dự án nào cần crawl")
            break
        n = ok + failed + 1
        log.info("[%s] %s (%s)", n, project.get("project_name"), project["project_id"])
        try:
            found, new, list_url = crawl_project(driver, db, project, max_pages, all_types)
            db.finish_source_job(project["project_id"], SOURCE_NAME, found, list_url)
            ok += 1
            consecutive_fails = 0
            log.info("   XONG: %s tin tìm thấy, %s tin mới", found, new)
        except BlockedError as exc:
            db.fail_source_job(project["project_id"], SOURCE_NAME, f"BlockedError: {exc}",
                               error_type="blocked")
            log.error("BỊ CHẶN: %s. Mở Chrome, vào batdongsan giải captcha rồi chạy lại.", exc)
            failed += 1
            break
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            db.fail_source_job(project["project_id"], SOURCE_NAME, msg, error_type=type(exc).__name__)
            failed += 1
            consecutive_fails += 1
            log.error("   LỖI: %s", msg[:200])
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                log.error("%s lỗi liên tiếp, có thể bị chặn. Dừng bước 1.", MAX_CONSECUTIVE_FAILS)
                break
        sleep_between(PROJECT_DELAY)

    log.info("Xong bước 1: %s dự án thành công, %s lỗi | tiến độ dự án: %s",
             ok, failed, db.source_progress().get(SOURCE_NAME, {}))


# =============================================================== bước 2: chi tiết tin
def parse_detail_html(soup: BeautifulSoup, row: Dict[str, Any]) -> Dict[str, Any]:
    """Bóc dữ liệu từ HTML trang chi tiết. Trả về dict khớp core.db_listings.LISTING_COLUMNS."""
    title = None
    for t in soup.select("h1.re__pr-title"):
        text = el_text(t)
        if text and "mã tin" not in text.lower():
            title = text
            break
    title = title or row.get("card_title")

    description = el_text(soup.select_one("#product-detail-web .re__pr-description .re__section-body"))

    posted = expired = price_raw = area_raw = None
    for item in soup.select(".re__pr-short-info-item"):
        label = (el_text(item.select_one(".title")) or "").lower()
        value = el_text(item.select_one(".value"))
        if "ngày đăng" in label:
            posted = value
        elif "ngày hết hạn" in label:
            expired = value
        elif "giá" in label:
            price_raw = value
        elif "diện tích" in label:
            area_raw = value

    bedrooms_raw = bathrooms_raw = None
    direction = balcony = legal = furniture =  address = None
    for spec in soup.select(".re__pr-specs-content-item"):
        key = (el_text(spec.select_one(".re__pr-specs-content-item-title")) or "").lower()
        val = el_text(spec.select_one(".re__pr-specs-content-item-value"))
        if not key or not val:
            continue
        if "phòng ngủ" in key:
            bedrooms_raw = val
        elif "phòng tắm" in key or "vệ sinh" in key:
            bathrooms_raw = val
        elif "hướng nhà" in key:
            direction = val
        elif "hướng ban công" in key:
            balcony = val
        elif "pháp lý" in key:
            legal = val
        elif "nội thất" in key:
            furniture = val
        elif "địa chỉ" in key:
            address = val

    # Giá / diện tích: ưu tiên bảng thông số, thiếu thì lấy từ card ở trang list
    price_raw = price_raw or row.get("card_price_raw")
    area_raw = area_raw or row.get("card_area_raw")

    # Tên dự án: lấy từ khối "Thông tin dự án" của tin (None nếu tin không thuộc dự án nào)
    project_name = el_text(soup.select_one(".re__project-card-info .re__project-title"))

    # Địa chỉ: dòng 1 là địa chỉ hiện tại, dòng 2 (nếu có) là đơn vị hành chính cũ trước sáp nhập
    addr1 = soup.select_one("#product-detail-web .re__address-line-1") or soup.select_one(".re__address-line-1")
    addr2 = soup.select_one("#product-detail-web .re__address-line-2") or soup.select_one(".re__address-line-2")
    address = el_text(addr1) or address
    address_old = (el_text(addr2) or "").strip("() ").strip() or None

    # Liên hệ
    contact_name = contact_phone = None
    phone_btn = soup.select_one(".re__link-phone, .js__phone")
    if phone_btn:
        contact_name = clean_text(phone_btn.get("data-kyc-name"))
        m = re.search(r"(\d{4}\s\d{3}\s\*+)", el_text(phone_btn.select_one("span")) or "")
        contact_phone = m.group(1) if m else None

    return {
        "title": title,
        "description_text": description,
        "price_raw": price_raw,
        "price_total_vnd": parse_price_vnd(price_raw),
        "area_raw": area_raw,
        "area_m2": parse_area_m2(area_raw),
        "bedrooms": parse_int(bedrooms_raw),
        "bathrooms": parse_int(bathrooms_raw),
        "direction": direction,
        "balcony_direction": balcony,
        "legal_status": legal,
        "furniture_status": furniture,
        "project_name_raw": project_name,
        "address_raw": address,
        "address_old_raw": address_old,
        "posted_at": parse_date_vn(posted),
        "expired_at": parse_date_vn(expired),
        "contact_name": contact_name,
        "contact_phone_masked": contact_phone,
    }


def is_gone_page(driver) -> bool:
    try:
        title = (driver.title or "").lower()
        url = (driver.current_url or "").lower()
    except Exception:
        return False
    return "/404" in url or "404" in title or "không tìm thấy" in title or "không tồn tại" in title


def crawl_listing_detail(driver, row: Dict[str, Any]) -> Dict[str, Any]:
    driver.get(row["listing_url"])
    try:
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#product-detail-web"))
        )
    except TimeoutException:
        if looks_blocked(driver):
            raise BlockedError("Trang chi tiết bị chặn")
        if is_gone_page(driver):
            raise ListingGone("Tin không còn tồn tại")
        raise
    sleep_between((1.0, 2.0))
    soup = BeautifulSoup(driver.page_source, "html.parser")
    return parse_detail_html(soup, row)


def run_detail(driver, db, limit: Optional[int] = None, max_attempts: int = 3) -> None:
    log.info("=" * 60)
    log.info("BƯỚC 2: CRAWL CHI TIẾT TIN -> crawl.listings")
    log.info("=" * 60)

    todo = db.fetch_pending_listings(SOURCE_NAME, limit, max_attempts)
    log.info("%s tin cần crawl chi tiết", len(todo))

    ok = failed = gone = consecutive_fails = 0
    for i, row in enumerate(todo, 1):
        lid = row["source_listing_id"]
        try:
            data = crawl_listing_detail(driver, row)
            db.save_listing(SOURCE_NAME, lid, row["project_id"], row["listing_url"], data, PARSER_VERSION)
            ok += 1
            consecutive_fails = 0
            log.info("[%s/%s] OK | %s | %s | %s | %s PN | %s",
                     i, len(todo), lid,
                     data.get("price_raw") or "-",
                     f"{data['area_m2']:g} m²" if data.get("area_m2") else "-",
                     data.get("bedrooms") if data.get("bedrooms") is not None else "-",
                     (data.get("title") or "")[:50])
        except ListingGone as exc:
            db.mark_listing_detail(SOURCE_NAME, lid, "gone", str(exc))
            gone += 1
            consecutive_fails = 0
            log.info("[%s/%s] %s: tin đã bị gỡ", i, len(todo), lid)
        except BlockedError as exc:
            log.error("BỊ CHẶN: %s. Mở Chrome, vào batdongsan giải captcha rồi chạy lại.", exc)
            break
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            db.mark_listing_detail(SOURCE_NAME, lid, "failed", msg)
            db.log_error("listing_detail", SOURCE_NAME, row["listing_url"], type(exc).__name__,
                         str(exc)[:500], ref_id=lid)
            failed += 1
            consecutive_fails += 1
            log.error("[%s/%s] %s: LỖI %s", i, len(todo), lid, msg[:150])
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                log.error("%s lỗi liên tiếp, có thể bị chặn. Dừng bước 2.", MAX_CONSECUTIVE_FAILS)
                break
        if i % 25 == 0:
            log.info("[%s/%s] đã xong: %s OK, %s gỡ, %s lỗi", i, len(todo), ok, gone, failed)
        sleep_between(DETAIL_DELAY)

    log.info("Xong bước 2: %s OK, %s đã gỡ, %s lỗi | tiến độ tin: %s",
             ok, gone, failed, db.listing_progress(SOURCE_NAME))


# =============================================================== main
def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl tin đăng batdongsan -> Supabase")
    parser.add_argument("stage", nargs="?", default="all", choices=["all", "list", "detail"])
    parser.add_argument("--limit", type=int, default=None,
                        help="Giới hạn số dự án (bước list) hoặc số tin (bước detail)")
    parser.add_argument("--max-pages", type=int, default=50, help="Số trang list tối đa mỗi dự án")
    parser.add_argument("--max-attempts", type=int, default=3, help="Số lần thử tối đa mỗi dự án/tin")
    parser.add_argument("--project-id", default=None, help="Chỉ chạy 1 dự án (vd DA_00001) để thử")
    parser.add_argument("--all-types", action="store_true",
                        help="Crawl chi tiết cả tin không phải căn hộ chung cư (mặc định: bỏ qua)")
    parser.add_argument("--stale-minutes", type=int, default=60,
                        help="Dự án 'running' quá số phút này được coi là bị kẹt và chạy lại")
    args = parser.parse_args()

    log_path = setup_logging("batdongsan_listings")
    log.info("Bắt đầu | stage=%s | log: %s", args.stage, log_path)

    with Db() as db:
        name, now = db.ping()
        log.info("Đã nối DB: %s (%s)", name, f"{now:%Y-%m-%d %H:%M:%S}")
        driver = connect_chrome()  # không quit để giữ Chrome đang mở

        if args.stage in ("all", "list"):
            run_list(driver, db, args.limit, args.max_pages, args.max_attempts,
                     args.all_types, args.project_id, args.stale_minutes)
        if args.stage in ("all", "detail"):
            run_detail(driver, db, args.limit, args.max_attempts)

        log.info("Tiến độ dự án: %s | tiến độ tin: %s",
                 db.source_progress().get(SOURCE_NAME, {}), db.listing_progress(SOURCE_NAME))


if __name__ == "__main__":
    main()