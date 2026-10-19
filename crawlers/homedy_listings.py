"""Crawl tin đăng homedy.com theo từng dự án -> Supabase.

Bước 1  list   : mỗi dự án trong crawl.project_sources (homedy) -> dò link tổng hợp tin bán
                 -> duyệt /p{n} -> crawl.listing_urls (không có link -> not_found)
Bước 2  detail : mỗi tin chưa xong trong crawl.listing_urls -> crawl.listings

    python -m crawlers.homedy_listings list --project-id DA_00001   # thử 1 dự án
    python -m crawlers.homedy_listings detail --limit 5             # thử 5 tin
    python -m crawlers.homedy_listings                              # cả 2 bước
"""
from __future__ import annotations

import argparse
import logging
import re
from urllib.parse import urljoin, urlparse

import unicodedata
from bs4 import BeautifulSoup

from core.db import Db
from core.errors import BlockedError, NotFoundError
from core.http import fetch_html, make_session
from core.logger import setup_logging
from core.parse import clean_text, el_text, parse_area_m2, parse_date_vn, parse_int, parse_price_vnd, slugify
from core.util import sleep_between, worker_name

SOURCE_NAME = "homedy"
PARSER_VERSION = "1.0.0"  # Sửa logic bóc dữ liệu thì đổi số này

BASE = "https://homedy.com"
MAX_CONSECUTIVE_FAILS = 5
PROBE_DELAY = (1.0, 2.0)
LIST_PAGE_DELAY = (2.0, 4.0)
DETAIL_DELAY = (1.0, 2.0)
PROJECT_DELAY = (3.0, 6.0)

PREFIXES = ("ban-can-ho-", "ban-can-ho-khu-do-thi-", "ban-can-ho-du-an-")
STRIP_RE = re.compile(r"^(chung-cu|can-ho|khu-do-thi|khu-dan-cu|du-an|kdc|kdt)-")
LISTING_RE = re.compile(r"-es(\d+)/?$")
CATEGORY_RE = re.compile(r"^(ban-can-ho|ban-nha-biet-thu-lien-ke|ban-dat-nen-du-an|ban-nha-pho-thuong-mai-shophouse)")
RANGE_SIDE = 0  # 0 = cận dưới, 1 = cận trên (giá và diện tích luôn cùng phía)
DIR_RE = r"(?:(Đông|Tây)[\s\-–]*(Nam|Bắc)|(Đông|Tây|Nam|Bắc))(?!\w)"

log = logging.getLogger("homedy.listings")


# ============================================================ tiện ích
def clean_url(href: str) -> str:
    return urlparse(urljoin(BASE, href))._replace(query="", fragment="").geturl()


def page_url(list_url: str, page: int) -> str:
    return list_url if page == 1 else f"{list_url.rstrip('/')}/p{page}"


def parse_cards(soup: BeautifulSoup) -> dict:
    """{listing_id: (url, title)} từ khối tab-content. Mỗi card có nhiều <a>, giữ title dài nhất."""
    box = soup.select_one("div.tab-content")
    found: dict = {}
    for a in (box.find_all("a", href=True) if box else []):
        url = clean_url(a["href"])
        m = LISTING_RE.search(urlparse(url).path)
        if not m:
            continue
        title = el_text(a) or ""
        old = found.get(m.group(1))
        if not old or len(title) > len(old[1]):
            found[m.group(1)] = (url, title)
    return found


def has_next_page(soup: BeautifulSoup, page: int) -> bool:
    nav = soup.select_one("div.page-nav")
    return bool(nav) and any(f"/p{page + 1}" in a["href"] for a in nav.find_all("a", href=True))


# ============================================================ bước 1: dò link dự án + thu URL
def slug_variants(name: str) -> list:
    full = slugify(name)
    if not full:
        return []
    core = STRIP_RE.sub("", full)
    urls = [f"{BASE}/ban-can-ho-{full}"] + [f"{BASE}/{p}{core}" for p in PREFIXES]
    return list(dict.fromkeys(urls))


def probe(session, url):
    """(url thật, soup) nếu trang tồn tại và có tin; ngược lại None.
    BlockedError/FetchError cố ý để nổi lên: lỗi tạm thời, không kết luận 'không có'."""
    try:
        soup, final = fetch_html(session, url)
    except NotFoundError:
        return None
    if urlparse(final).path.rstrip("/") != urlparse(url).path.rstrip("/"):
        return None
    return (final, soup) if parse_cards(soup) else None


def find_project_list_url(session, project_name):
    for url in slug_variants(project_name):
        hit = probe(session, url)
        sleep_between(PROBE_DELAY)
        if hit:
            return hit
    return None


def crawl_project(session, db, job, max_pages, all_types):
    """Trả về (số tin, số tin mới, link list) hoặc None nếu dự án không có trang trên homedy."""
    pid = job["project_id"]
    first = None
    list_url = job.get("listing_list_url")
    if not list_url:
        hit = find_project_list_url(session, job["project_name"])
        if not hit:
            return None
        list_url, first = hit[0], hit[1]
        db.set_listing_list_url(pid, SOURCE_NAME, list_url)
        log.info("   Link tin đăng: %s", list_url)

    seen: set = set()
    new_total = 0
    for page in range(1, max_pages + 1):
        url = page_url(list_url, page)
        if page == 1 and first is not None:
            soup = first  # tái dùng trang đã tải lúc dò
        else:
            try:
                soup, _ = fetch_html(session, url)
            except NotFoundError:
                break  # hết trang
        cards = []
        for lid, (curl, title) in parse_cards(soup).items():
            if lid in seen:
                continue
            cat = CATEGORY_RE.match(urlparse(curl).path.strip("/").split("/")[0])
            cat = cat.group(1) if cat else None
            cards.append({
                "source_name": SOURCE_NAME, "source_listing_id": lid, "listing_url": curl,
                "project_id": pid, "category_slug": cat, "is_apartment": cat == "ban-can-ho",
                "list_page_url": url, "page_number": page, "vip_type": None, "card_title": title,
                "card_price_raw": None, "card_area_raw": None, "card_location_raw": None,
                "card_posted_raw": None,
            })
        log.info("   Trang %s: %s tin mới trong lần chạy", page, len(cards))
        if not cards:
            break
        seen.update(c["source_listing_id"] for c in cards)
        new_total += db.save_listing_urls(cards, all_types)
        if not has_next_page(soup, page):
            break
        sleep_between(LIST_PAGE_DELAY)
    else:
        log.warning("   Chạm giới hạn --max-pages=%s, có thể còn tin chưa lấy", max_pages)
    return len(seen), new_total, list_url


def run_list(session, db, limit=None, max_pages=50, max_attempts=3, all_types=False,
             project_id=None, stale_minutes=60):
    log.info("BƯỚC 1: DÒ LINK DỰ ÁN + THU URL TIN -> crawl.listing_urls")
    reset = db.release_stale_jobs(SOURCE_NAME, stale_minutes)
    if reset:
        log.info("Đã đưa %s dự án 'running' bị kẹt về 'pending'", reset)

    worker = worker_name()
    if project_id:
        limit = 1
    ok = failed = missing = consecutive_fails = 0
    while limit is None or (ok + failed + missing) < limit:
        job = db.claim_source_job(SOURCE_NAME, worker, max_attempts, project_id)
        if not job:
            log.info("Không còn dự án nào cần crawl")
            break
        log.info("[%s] %s (%s)", ok + failed + missing + 1, job.get("project_name"), job["project_id"])
        try:
            res = crawl_project(session, db, job, max_pages, all_types)
            if res is None:
                db.mark_source_not_found(job["project_id"], SOURCE_NAME)
                missing += 1
                log.info("   Không có trang dự án trên homedy")
            else:
                found, new, list_url = res
                db.finish_source_job(job["project_id"], SOURCE_NAME, found, list_url)
                ok += 1
                log.info("   XONG: %s tin, %s mới", found, new)
            consecutive_fails = 0
        except BlockedError as exc:
            db.fail_source_job(job["project_id"], SOURCE_NAME, f"BlockedError: {exc}", "blocked")
            log.error("BỊ CHẶN: %s. Dừng, chờ một lúc rồi chạy lại.", exc)
            break
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            db.fail_source_job(job["project_id"], SOURCE_NAME, msg, type(exc).__name__)
            failed += 1
            consecutive_fails += 1
            log.error("   LỖI: %s", msg[:200])
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                log.error("%s lỗi liên tiếp, dừng bước 1.", MAX_CONSECUTIVE_FAILS)
                break
        sleep_between(PROJECT_DELAY)

    log.info("Xong bước 1: %s OK, %s không có trang, %s lỗi | %s",
             ok, missing, failed, db.source_progress().get(SOURCE_NAME, {}))


# ============================================================ bước 2: chi tiết tin
def pick_range(raw, side=RANGE_SIDE):
    """'5 - 7 Tỷ' -> '5 Tỷ' ; '75 - 97 m2' -> '75 m2'. Không phải khoảng thì giữ nguyên."""
    parts = re.split(r"\s*[-–—]\s*", (raw or "").strip(), maxsplit=1)
    if len(parts) < 2:
        return raw
    part = parts[side]
    if re.search(r"[^\W\d_]", part):  # đã có đơn vị
        return part
    return f"{part} {re.sub(r'^[\d.,\s]+', '', parts[1])}"


def _dir_from_match(m):
    """Chuẩn hóa về: 'Đông', 'Đông Bắc', 'Tây Nam'..."""
    return (f"{m.group(1)} {m.group(2)}" if m.group(1) else m.group(3)).title()


def norm_direction(v):
    m = re.match(DIR_RE, unicodedata.normalize("NFC", (v or "").strip()), re.I)
    return _dir_from_match(m) if m else None


def find_direction(*sources):
    """Khối thuộc tính không có hướng -> tìm cụm 'hướng + hướng hợp lệ' trong tiêu đề rồi mô tả."""
    for s in sources:
        m = re.search(r"hướng\s*:?\s*" + DIR_RE, unicodedata.normalize("NFC", s or ""), re.I)
        if m:
            return _dir_from_match(m)
    return None


def parse_detail(soup: BeautifulSoup, row: dict) -> dict:
    text = soup.get_text("\n", strip=True)

    def rx(pattern, flags=re.I):
        m = re.search(pattern, text, flags)
        return clean_text(m.group(1)) if m else None

    short = {}
    for it in soup.select("div.short-item"):
        label = (el_text(it.select_one("span")) or "").lower()
        short[label] = el_text(it.find("strong"))
    price_raw = short.get("giá") or row.get("card_price_raw")
    area_raw = short.get("diện tích") or row.get("card_area_raw")

    attrs = {}
    for it in soup.select("div.product-attributes--item"):
        sp = it.find_all("span")
        if len(sp) >= 2:
            attrs[(el_text(sp[0]) or "").lower()] = el_text(sp[1])

    def attr(*keys):
        return next((v for k, v in attrs.items() if v and any(x in k for x in keys)), None)

    title = el_text(soup.select_one("h1")) or row.get("card_title")
    description = rx(r"Thông tin mô tả\s*(.*?)\s*Loại hình", re.I | re.S)
    blob = f"{title or ''} {description or ''}"  # chỉ tìm trong nội dung của chính tin này

    def from_blob(pattern):
        m = re.search(pattern, blob, re.I)
        return int(m.group(1)) if m else None

    addr = soup.select_one("div.address")
    address_raw = None
    if addr:
        address_raw = ", ".join(filter(None, (el_text(s) for s in addr.find_all("span")))) or el_text(addr)

    vis, star = soup.select_one(".mobile-visible-part"), soup.select_one(".mobile-star")
    phone = f"{el_text(vis)}{el_text(star) or '***'}" if vis and el_text(vis) else None

    return {
        "title": title,
        "description_text": description,
        "price_raw": price_raw,
        "price_total_vnd": parse_price_vnd(pick_range(price_raw)),
        "area_raw": area_raw,
        "area_m2": parse_area_m2(pick_range(area_raw)),
        "bedrooms": parse_int(attr("phòng ngủ")) or from_blob(r"(?<!\d)(\d{1,2})\s*(?:PN|phòng ngủ)(?!\w)"),
        "bathrooms": parse_int(attr("phòng tắm", "vệ sinh")) or from_blob(r"(?<!\d)(\d{1,2})\s*(?:WC|phòng tắm|vệ sinh)(?!\w)"),
        "direction": norm_direction(attr("hướng nhà", "hướng cửa")) or find_direction(title, description),
        "balcony_direction": norm_direction(attr("hướng ban công")),
        "legal_status": attr("pháp lý") or rx(r"Tình trạng pháp lý\s*([^\n]+)"),
        "furniture_status": attr("nội thất"),
        "project_name_raw": rx(r"Thuộc dự án\s*([^\n]+)"),
        "address_raw": address_raw,
        "address_old_raw": None,
        "posted_at": parse_date_vn(rx(r"Ngày đăng\s*([^\n]+)")),
        "expired_at": parse_date_vn(rx(r"Ngày hết hạn\s*([^\n]+)")),
        "contact_name": rx(r"Liên hệ tư vấn\s*([^\n]+)"),
        "contact_phone_masked": phone,
    }


def run_detail(session, db, limit=None, max_attempts=3):
    log.info("BƯỚC 2: CRAWL CHI TIẾT TIN -> crawl.listings")
    todo = db.fetch_pending_listings(SOURCE_NAME, limit, max_attempts)
    log.info("%s tin cần crawl chi tiết", len(todo))

    ok = failed = gone = consecutive_fails = 0
    for i, row in enumerate(todo, 1):
        lid = row["source_listing_id"]
        try:
            soup, _ = fetch_html(session, row["listing_url"])
            data = parse_detail(soup, row)
            db.save_listing(SOURCE_NAME, lid, row["project_id"], row["listing_url"], data, PARSER_VERSION)
            ok += 1
            consecutive_fails = 0
            log.info("[%s/%s] OK | %s | %s | %s | %s", i, len(todo), lid, data["price_raw"] or "-",
                     f"{data['area_m2']:g} m²" if data["area_m2"] else "-", (data["title"] or "")[:50])
        except NotFoundError as exc:
            db.mark_listing_detail(SOURCE_NAME, lid, "gone", str(exc))
            gone += 1
            consecutive_fails = 0
            log.info("[%s/%s] %s: tin đã bị gỡ", i, len(todo), lid)
        except BlockedError as exc:
            log.error("BỊ CHẶN: %s. Dừng, chờ một lúc rồi chạy lại.", exc)
            break
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            db.mark_listing_detail(SOURCE_NAME, lid, "failed", msg)
            db.log_error("listing_detail", SOURCE_NAME, row["listing_url"], type(exc).__name__, msg, ref_id=lid)
            failed += 1
            consecutive_fails += 1
            log.error("[%s/%s] %s: LỖI %s", i, len(todo), lid, msg[:150])
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                log.error("%s lỗi liên tiếp, dừng bước 2.", MAX_CONSECUTIVE_FAILS)
                break
        sleep_between(DETAIL_DELAY)

    log.info("Xong bước 2: %s OK, %s đã gỡ, %s lỗi | %s", ok, gone, failed, db.listing_progress(SOURCE_NAME))


# ============================================================ main
def main() -> None:
    p = argparse.ArgumentParser(description="Crawl tin đăng homedy -> Supabase")
    p.add_argument("stage", nargs="?", default="all", choices=["all", "list", "detail"])
    p.add_argument("--limit", type=int, default=None, help="Số dự án (list) hoặc số tin (detail)")
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--project-id", default=None, help="Chỉ chạy 1 dự án (vd DA_00001)")
    p.add_argument("--all-types", action="store_true", help="Crawl chi tiết cả tin không phải căn hộ")
    p.add_argument("--stale-minutes", type=int, default=60)
    args = p.parse_args()

    log_path = setup_logging("homedy_listings")
    log.info("Bắt đầu | stage=%s | log: %s", args.stage, log_path)

    session = make_session()
    with Db() as db:
        name, now = db.ping()
        log.info("Đã nối DB: %s (%s)", name, f"{now:%Y-%m-%d %H:%M:%S}")
        if args.stage in ("all", "list"):
            run_list(session, db, args.limit, args.max_pages, args.max_attempts,
                     args.all_types, args.project_id, args.stale_minutes)
        if args.stage in ("all", "detail"):
            run_detail(session, db, args.limit, args.max_attempts)


if __name__ == "__main__":
    main()