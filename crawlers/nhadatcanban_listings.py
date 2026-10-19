"""Crawl tin đăng nhadatcanban.com.vn theo từng dự án -> Supabase.

Bước 1  list   : mỗi dự án trong crawl.project_sources (nhadatcanban) -> tìm theo từ khóa tên dự án
                 -> giữ tin có tên dự án trong title -> crawl.listing_urls (không có tin -> not_found)
Bước 2  detail : mỗi tin chưa xong trong crawl.listing_urls -> crawl.listings

    python -m crawlers.nhadatcanban_listings list --project-id DA_00001   # thử 1 dự án
    python -m crawlers.nhadatcanban_listings detail --limit 5             # thử 5 tin
    python -m crawlers.nhadatcanban_listings                              # cả 2 bước

Cách tìm (site không có trang riêng cho từng dự án):
    1. Từ tên dự án sinh danh sách từ khóa, vd "lumiere-boulevard".
    2. Gọi /can-ho-can-ban-co-tu-khoa-<kw>[/page-N]. Trang tìm kiếm thật có <h1> chứa "có từ khóa";
       không có thì site đã trả trang mặc định => từ khóa không được phục vụ, thử từ khóa khác.
    3. Site độn tin không liên quan khi hết kết quả khớp => dừng sau STOP_AFTER_EMPTY trang liên tiếp
       không có tin nào khớp tên dự án trong title.
    4. Tìm toàn quốc chỉ trả ~20 tin, nên lặp lại từ khóa tốt nhất theo từng tỉnh có tin khớp
       (vùng HCM sau sáp nhập = HCM + Bình Dương + BR-VT cũ).
"""
from __future__ import annotations

import argparse
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.db import Db
from core.errors import BlockedError, CrawlError, FetchError, NotFoundError
from core.http import fetch_html, make_session
from core.logger import setup_logging
from core.parse import parse_area_m2, parse_date_vn, parse_price_vnd
from core.util import sleep_between, worker_name

SOURCE_NAME = "nhadatcanban"
PARSER_VERSION = "2.0.0"  # Sửa logic bóc dữ liệu thì đổi số này

BASE_URL = "https://nhadatcanban.com.vn"
CATEGORY_SLUG = "can-ho-can-ban"
# Sau sáp nhập "TP.HCM" gồm HCM + Bình Dương + BR-VT cũ; site vẫn chia theo tỉnh cũ.
REGION_CITY_SLUGS = ("ho-chi-minh", "binh-duong", "ba-ria-vung-tau")
BRAND_PREFIXES = {"vinhomes", "vin"}

MAX_CONSECUTIVE_FAILS = 5
MAX_KEYWORDS = 5
PROVINCE_DEEPEN_MIN_HITS = 8   # theo tỉnh: trang 1 có từng này tin khớp mới đi tiếp trang sau
STOP_AFTER_EMPTY = 2

REQUEST_DELAY = (1.0, 2.0)     # nghỉ trước mỗi request
PROJECT_DELAY = (3.0, 6.0)     # nghỉ giữa các dự án
DETAIL_DELAY = (1.0, 2.0)      # nghỉ giữa các tin

log = logging.getLogger("nhadatcanban.listings")


class KeywordNotServed(CrawlError):
    """Mọi từ khóa đều bị site trả trang mặc định (cần slug riêng cho dự án này)."""


# ============================================================ chuẩn hóa văn bản
def norm(s: Optional[str]) -> str:
    """Không dấu, lowercase, chỉ còn a-z0-9 và dấu cách."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("đ", "d").replace("Đ", "D").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def contains_phrase(text_norm: str, phrase_norm: str) -> bool:
    if not text_norm or not phrase_norm:
        return False
    return f" {phrase_norm} " in f" {text_norm} "


# ============================================================ tên dự án -> biến thể -> từ khóa
@dataclass(frozen=True)
class NameVariant:
    text: str  # đã chuẩn hóa
    kind: str  # full | short


_NAME_PREFIXES = ("chung cu", "can ho", "nha o xa hoi", "khu can ho", "du an")
_TRAILING_PLACES = (
    "da nang", "danang", "ha noi", "hanoi", "hai phong", "quy nhon", "nha trang", "ha long",
    "phu quoc", "vung tau", "thanh hoa", "bac giang", "sapa", "hue", "phu my hung", "thuan an",
    "phuc yen", "van giang", "gia lam", "long bien", "ecopark",
)
_TRAILING_GENERIC = ("apartment", "residences", "residence")


def _strip_trailing(text: str, phrases: Iterable[str], min_tokens: int) -> str:
    toks = text.split()
    for ph in sorted(phrases, key=lambda x: -len(x.split())):
        pt = ph.split()
        if len(toks) - len(pt) >= min_tokens and toks[-len(pt):] == pt:
            return " ".join(toks[: -len(pt)])
    return text


def shorten_chain(project_name: str) -> list[str]:
    """'The Opus One - Vinhomes Grand Park' -> '... grand park', 'the opus one', 'opus one'."""
    full = norm(project_name)
    chain = [full]
    cur = full

    def push(t: str) -> None:
        nonlocal cur
        t = t.strip()
        if t and t != cur and len(t.replace(" ", "")) >= 4:
            cur = t
            chain.append(t)

    push(norm(re.split(r"\s+[-–—]\s+", project_name.strip())[0]))
    for p in _NAME_PREFIXES:
        if cur.startswith(p + " "):
            push(cur[len(p) + 1:])
            break
    push(_strip_trailing(cur, _TRAILING_PLACES, 2))
    push(_strip_trailing(cur, _TRAILING_GENERIC, 2))
    toks = cur.split()
    if len(toks) >= 2 and toks[0] in BRAND_PREFIXES:
        push(" ".join(toks[1:]))
    toks = cur.split()
    if len(toks) >= 3 and toks[0] == "the":
        push(" ".join(toks[1:]))
    return chain


def build_variants(project_name: str) -> list[NameVariant]:
    chain = shorten_chain(project_name)
    out = [NameVariant(chain[0], "full")]
    seen = {chain[0]}
    for t in chain[1:]:
        if t not in seen:
            seen.add(t)
            out.append(NameVariant(t, "short"))
    return out


def build_keywords(variants: list[NameVariant]) -> list[str]:
    return [v.text.replace(" ", "-") for v in variants]


def keyword_from_url_or_slug(text: Optional[str]) -> Optional[str]:
    """Lấy từ khóa từ slug hoặc link đã lưu: '.../can-ho-can-ban-tai-x-co-tu-khoa-<kw>/page-2' -> '<kw>'."""
    text = (text or "").strip().strip("/")
    if not text:
        return None
    if "co-tu-khoa-" in text:
        text = text.split("co-tu-khoa-", 1)[1]
    text = text.split("/page-")[0]
    return norm(text).replace(" ", "-") or None


def build_search_url(city_slug: Optional[str], keyword: str, page: int = 1) -> str:
    where = f"-tai-{city_slug}" if city_slug else ""
    url = f"{BASE_URL}/can-ho-can-ban{where}-co-tu-khoa-{keyword}"
    return url if page <= 1 else f"{url}/page-{page}"


def title_match(title: str, variants: list[NameVariant]) -> str:
    """strong: tên đầy đủ trong title | medium: tên rút gọn trong title | none."""
    t = norm(title)
    if any(v.kind == "full" and contains_phrase(t, v.text) for v in variants):
        return "strong"
    if any(v.kind == "short" and contains_phrase(t, v.text) for v in variants):
        return "medium"
    return "none"


# ============================================================ parse giá trị riêng của site
def parse_price(text: Optional[str]) -> Optional[int]:
    """Dùng core.parse, chỉ quy đổi thêm cách viết 'tỉ' / 'ngàn' của site."""
    if not text:
        return None
    return parse_price_vnd(text.lower().replace("tỉ", "tỷ").replace("ngàn", "nghìn"))


def _first_int(pattern: str, *texts: Optional[str], max_value: int = 20) -> Optional[int]:
    for t in texts:
        if not t:
            continue
        m = re.search(pattern, t, re.I)
        if m and 0 < int(m.group(1)) <= max_value:
            return int(m.group(1))
    return None


def parse_bedrooms(*texts: Optional[str]) -> Optional[int]:
    return _first_int(r"(\d+)\s*(?:pn|phòng ngủ|phong ngu)", *texts)


def parse_bathrooms(*texts: Optional[str]) -> Optional[int]:
    return _first_int(r"(\d+)\s*(?:wc|phòng tắm|phong tam|toilet)", *texts)


def parse_furniture(text: Optional[str]) -> Optional[str]:
    t = norm(text)
    if re.search(r"(full|day du|cao cap) noi that|noi that (full|day du|cao cap)", t):
        return "full"
    if re.search(r"noi that co ban|co ban noi that", t):
        return "basic"
    if re.search(r"khong noi that|nha trong|ban giao tho", t):
        return "none"
    return None


_PHONE_RX = re.compile(r"(?<!\d)(?:\+?84|0)[\s.\-]?\d(?:[\s.\-]?\d){8}(?!\d)")


def mask_phones_in_text(text: Optional[str]) -> Optional[str]:
    return _PHONE_RX.sub("[SĐT]", text) if text else text


def mask_phone(phone: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", phone or "")
    return digits[:-4] + "****" if len(digits) >= 7 else None


def _clean(s: Optional[str]) -> Optional[str]:
    s = (s or "").strip()
    return s or None


# ============================================================ parse trang tìm kiếm
@dataclass
class ListItem:
    listing_id: str
    url: str
    title: str
    price_text: str
    area_text: str
    district_text: str
    posted_hint: str
    rank: int


@dataclass
class SearchPage:
    is_search_page: bool
    items: list[ListItem] = field(default_factory=list)
    has_next: bool = False


def parse_search_page(soup: BeautifulSoup) -> SearchPage:
    h1 = soup.select_one("h1")
    page = SearchPage(is_search_page=bool(h1 and "có từ khóa" in h1.get_text()))
    if not page.is_search_page:
        return page

    for rank, art in enumerate(soup.select("div.list > article"), start=1):
        a = art.select_one("h4 a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        m = re.search(r"-(\d+)\.html", href)
        if not m:
            continue
        spans = art.find_all("span")
        district = art.select_one('a[href^="nha-dat-tai-"]')
        time_tag = art.select_one("time")
        page.items.append(ListItem(
            listing_id=m.group(1),
            url=urljoin(BASE_URL + "/", href),
            title=(a.get("title") or a.get_text(" ", strip=True)).strip(),
            price_text=spans[0].get_text(strip=True) if len(spans) > 0 else "",
            area_text=spans[1].get_text(strip=True) if len(spans) > 1 else "",
            district_text=district.get_text(strip=True) if district else "",
            posted_hint=time_tag.get_text(strip=True) if time_tag else "",
            rank=rank,
        ))
    page.has_next = bool(soup.select_one('#paginator a[rel="next"]'))
    return page


def province_slug(district_text: Optional[str]) -> Optional[str]:
    """'Quận 1, Hồ Chí Minh' -> 'ho-chi-minh'."""
    if not district_text:
        return None
    return norm(district_text.split(",")[-1]).replace(" ", "-") or None


# ============================================================ bước 1: danh sách tin
@dataclass
class KeywordReport:
    keyword: str
    city: Optional[str] = None
    status: str = "served"  # served | keyword_not_served | fetch_failed | partial
    pages: int = 0
    title_hits: int = 0


def _get_soup(session, url: str) -> Optional[BeautifulSoup]:
    """Tải 1 trang có nghỉ. None nếu lỗi mạng tạm thời. BlockedError cho chạy lên trên."""
    sleep_between(REQUEST_DELAY)
    try:
        soup, _ = fetch_html(session, url)
        return soup
    except FetchError as exc:
        log.warning("Lỗi mạng %s: %s", url, exc)
        return None


def _fetch_search(session, kw: str, city: Optional[str], page_no: int):
    """-> (status, SearchPage | None)."""
    try:
        soup = _get_soup(session, build_search_url(city, kw, page_no))
    except NotFoundError:
        return "keyword_not_served", None
    if soup is None:
        return "fetch_failed", None
    sp = parse_search_page(soup)
    return ("served", sp) if sp.is_search_page else ("keyword_not_served", None)


@dataclass
class _Found:
    item: ListItem
    keyword: str
    city: Optional[str]
    page: int
    level: str


def crawl_project(session, job: dict, max_pages: int, extra_city_slugs: Iterable[str] = REGION_CITY_SLUGS):
    """Trả về (danh sách card đã khớp title, danh sách KeywordReport, link tìm kiếm tốt nhất)."""
    project_id, name = job["project_id"], job["project_name"]
    variants = build_variants(name)

    keywords: list[str] = []
    preferred = [keyword_from_url_or_slug(job.get("slug")), keyword_from_url_or_slug(job.get("listing_list_url"))]
    for k in [*preferred, *build_keywords(variants)]:
        if k and k not in keywords:
            keywords.append(k)
    keywords = keywords[:MAX_KEYWORDS]

    reports: list[KeywordReport] = []
    found: dict[str, _Found] = {}

    def ingest(sp: SearchPage, kw: str, city: Optional[str], page_no: int):
        hits = strong = 0
        for it in sp.items:
            lvl = title_match(it.title, variants)
            hits += lvl != "none"
            strong += lvl == "strong"
            if it.listing_id not in found:
                found[it.listing_id] = _Found(it, kw, city, page_no, lvl)
        return hits, strong

    def deepen(rep: KeywordReport, kw: str, city: Optional[str], sp: SearchPage, first_hits: int):
        empty = 0 if first_hits else 1
        page_no = 1
        while empty < STOP_AFTER_EMPTY and sp.has_next and page_no < max_pages:
            page_no += 1
            _, nxt = _fetch_search(session, kw, city, page_no)
            if nxt is None:
                rep.status = "partial"
                return
            rep.pages += 1
            h, _ = ingest(nxt, kw, city, page_no)
            rep.title_hits += h
            empty = 0 if h else empty + 1
            sp = nxt

    # Bước A: thăm dò trang 1 của từng từ khóa (toàn quốc)
    probes = []
    for kw in keywords:
        rep = KeywordReport(keyword=kw)
        rep.status, sp = _fetch_search(session, kw, None, 1)
        if sp is not None:
            rep.pages = 1
            rep.title_hits, strong = ingest(sp, kw, None, 1)
            probes.append((rep, kw, sp, rep.title_hits, strong))
        reports.append(rep)
        log.info("   kw=%s status=%s title_hits=%s", kw, rep.status, rep.title_hits)

    best_kw = None
    useful = [p for p in probes if p[3] > 0]
    if useful:
        # strong nặng gấp 3; hòa thì ưu tiên từ khóa dài hơn
        best = max(useful, key=lambda p: (p[4] * 3 + (p[3] - p[4]), len(p[1])))
        _, best_kw, sp, hits, _ = best
        deepen(best[0], best_kw, None, sp, hits)

        # Bước B: lặp lại từ khóa tốt nhất theo từng tỉnh có tin khớp
        hit_found = [f for f in found.values() if f.level != "none"]
        provinces: list[str] = []
        unknown = False
        for f in hit_found:
            ps = province_slug(f.item.district_text)
            if ps is None:
                unknown = True
            elif ps not in provinces:
                provinces.append(ps)
        region = tuple(extra_city_slugs or ())
        if region and (unknown or any(p in region for p in provinces)):
            provinces += [r for r in region if r not in provinces]
        for c in provinces:
            rep = KeywordReport(keyword=best_kw, city=c)
            rep.status, sp = _fetch_search(session, best_kw, c, 1)
            if sp is not None:
                rep.pages = 1
                rep.title_hits, _ = ingest(sp, best_kw, c, 1)
                if rep.title_hits >= PROVINCE_DEEPEN_MIN_HITS:
                    deepen(rep, best_kw, c, sp, rep.title_hits)
            reports.append(rep)
            log.info("   kw=%s city=%s status=%s title_hits=%s", best_kw, c, rep.status, rep.title_hits)

    cards = []
    for f in found.values():
        if f.level == "none":
            continue
        it = f.item
        cards.append({
            "source_name": SOURCE_NAME, "source_listing_id": it.listing_id, "listing_url": it.url,
            "project_id": project_id, "category_slug": CATEGORY_SLUG, "is_apartment": True,
            "list_page_url": build_search_url(f.city, f.keyword, f.page), "page_number": f.page,
            "vip_type": None, "card_title": mask_phones_in_text(it.title),
            "card_price_raw": it.price_text or None, "card_area_raw": it.area_text or None,
            "card_location_raw": it.district_text or None, "card_posted_raw": it.posted_hint or None,
        })
    list_url = build_search_url(None, best_kw, 1) if best_kw else None
    return cards, reports, list_url


def run_list(session, db, limit=None, max_pages=20, max_attempts=3, all_types=False,
             project_id=None, stale_minutes=60):
    log.info("BƯỚC 1: TÌM TIN THEO TỪ KHÓA DỰ ÁN -> crawl.listing_urls")
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
        pid = job["project_id"]
        log.info("[%s] %s (%s)", ok + failed + missing + 1, job.get("project_name"), pid)
        try:
            cards, reports, list_url = crawl_project(session, job, max_pages)
            statuses = [r.status for r in reports]
            any_ok = any(s in ("served", "partial") for s in statuses)
            net_issue = any(s in ("fetch_failed", "partial") for s in statuses)
            summary = " | ".join(f"{r.keyword}@{r.city or 'all'}:{r.status}({r.title_hits})" for r in reports)

            if not any_ok:
                if "fetch_failed" in statuses:
                    raise FetchError(f"lỗi mạng: {summary}")
                raise KeywordNotServed(summary)
            if not cards:
                db.mark_source_not_found(pid, SOURCE_NAME)
                missing += 1
                log.info("   Không có tin nào khớp tên dự án")
            else:
                new = db.save_listing_urls(cards, all_types)
                if net_issue:  # đã lưu phần có được; đánh failed để lần sau chạy lại cho đủ
                    raise FetchError(f"thiếu một phần ({len(cards)} tin đã lưu): {summary}")
                db.finish_source_job(pid, SOURCE_NAME, len(cards), list_url)
                ok += 1
                log.info("   XONG: %s tin, %s mới", len(cards), new)
            consecutive_fails = 0
        except BlockedError as exc:
            db.fail_source_job(pid, SOURCE_NAME, f"BlockedError: {exc}", "blocked")
            log.error("BỊ CHẶN: %s. Dừng, chờ một lúc rồi chạy lại.", exc)
            break
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            db.fail_source_job(pid, SOURCE_NAME, msg, type(exc).__name__)
            failed += 1
            log.error("   LỖI: %s", msg[:200])
            if not isinstance(exc, KeywordNotServed):  # từ khóa bị chặn là lỗi của dự án, không phải bị chặn IP
                consecutive_fails += 1
                if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                    log.error("%s lỗi liên tiếp, dừng bước 1.", MAX_CONSECUTIVE_FAILS)
                    break
        sleep_between(PROJECT_DELAY)

    log.info("Xong bước 1: %s OK, %s không có tin, %s lỗi | %s",
             ok, missing, failed, db.source_progress().get(SOURCE_NAME, {}))


# ============================================================ bước 2: chi tiết tin
_AD_SEPARATOR = re.compile(r"^[=_*~\-]{4,}")


def parse_detail(soup: BeautifulSoup, row: dict) -> dict:
    """Bóc HTML trang chi tiết. Trả về dict khớp core.db.LISTING_COLUMNS."""
    for tag in soup.select(".google-anno-skip, .google-anno-sc"):
        tag.decompose()
    art = soup.select_one("main article") or soup

    h1 = art.select_one("h1")
    title = h1.get_text(" ", strip=True) if h1 else None
    if not title:
        raise ValueError("Không thấy <h1> (cấu trúc trang đổi hoặc trang lỗi)")

    info = art.select_one("div.info")
    spans = info.find_all("span") if info else []
    price_raw = (spans[0].get_text(strip=True) if len(spans) > 0 else None) or row.get("card_price_raw")
    area_raw = (spans[1].get_text(strip=True) if len(spans) > 1 else None) or row.get("card_area_raw")

    # Mô tả: cắt phần quảng cáo/tuyển dụng của môi giới sau dòng "======"
    det = art.select_one("div.detail")
    lines = [ln.strip() for ln in det.get_text("\n").split("\n")] if det else []
    kept = []
    for ln in (x for x in lines if x):
        if _AD_SEPARATOR.match(ln):
            break
        kept.append(ln)
    description = "\n".join(kept) or None

    props = {}
    for li in art.select("ul > li"):
        label, strong = li.find("label"), li.find("strong")
        if label and strong:
            props[norm(label.get_text(" ", strip=True))] = strong.get_text(" ", strip=True)

    date_div = art.select_one("div.date")
    posted_raw = _clean(date_div.get_text(" ", strip=True)) if date_div else None
    rooms = _clean(props.get("phong oc"))
    address = _clean(props.get("dia chi"))

    return {
        "title": mask_phones_in_text(title),
        "description_text": mask_phones_in_text(description),
        "price_raw": price_raw,
        "price_total_vnd": parse_price(price_raw),
        "area_raw": area_raw,
        "area_m2": parse_area_m2(area_raw),
        "bedrooms": parse_bedrooms(rooms, description, title),
        "bathrooms": parse_bathrooms(rooms, description, title),
        "direction": _clean(props.get("huong")),
        "balcony_direction": None,
        "legal_status": _clean(props.get("phap ly")),
        "furniture_status": parse_furniture(description),
        "project_name_raw": None,  # site không có trường dự án trên tin
        "address_raw": address,
        "address_old_raw": address,  # site vẫn dùng đơn vị hành chính cũ
        "posted_at": parse_date_vn(posted_raw),
        "expired_at": None,
        "contact_name": _clean(props.get("nguoi dang")),
        "contact_phone_masked": mask_phone(props.get("dien thoai")),
    }


def run_detail(session, db, limit=None, max_attempts=3):
    log.info("BƯỚC 2: CRAWL CHI TIẾT TIN -> crawl.listings")
    todo = db.fetch_pending_listings(SOURCE_NAME, limit, max_attempts)
    log.info("%s tin cần crawl chi tiết", len(todo))

    ok = failed = gone = consecutive_fails = 0
    for i, row in enumerate(todo, 1):
        lid = row["source_listing_id"]
        try:
            sleep_between(REQUEST_DELAY)
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
    p = argparse.ArgumentParser(description="Crawl tin đăng nhadatcanban -> Supabase")
    p.add_argument("stage", nargs="?", default="all", choices=["all", "list", "detail"])
    p.add_argument("--limit", type=int, default=None, help="Số dự án (list) hoặc số tin (detail)")
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--project-id", default=None, help="Chỉ chạy 1 dự án (vd DA_00001)")
    p.add_argument("--all-types", action="store_true", help="Crawl chi tiết cả tin không phải căn hộ")
    p.add_argument("--stale-minutes", type=int, default=60)
    args = p.parse_args()

    log_path = setup_logging("nhadatcanban_listings")
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