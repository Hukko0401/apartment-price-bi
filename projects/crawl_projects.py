"""Crawl dự án chung cư trên batdongsan.com.vn -> Supabase.

Bước 1  urls     : quét danh sách "Sắp mở bán" + "Đang mở bán" -> crawl.project_urls (tự sinh project_id)
Bước 2  details  : vào từng URL chưa xong -> crawl.projects

Chạy từ THƯ MỤC GỐC của repo:
    python -m projects.crawl_projects                      # cả 2 bước
    python -m projects.crawl_projects urls --max-pages 2   # chạy thử bước 1
    python -m projects.crawl_projects details --limit 5    # chạy thử bước 2

Cần mở sẵn Chrome ở chế độ debug (xem README).
"""
import argparse
import random
import re
import time

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from core.db import Db
from core.parse import slugify

SOURCE_NAME = "batdongsan"
PARSER_VERSION = "1.0.0"  # Sửa logic bóc dữ liệu thì đổi số này (vd 1.0.0 -> 1.1.0)

BASE_URL = "https://batdongsan.com.vn/du-an-can-ho-chung-cu"
QUERY_PARAMS = "?sts=2,1"
CHROME_DEBUG_ADDR = "127.0.0.1:9222"
MAX_CONSECUTIVE_FAILS = 5  # lỗi liên tiếp quá số này thì dừng (nghi bị chặn)

PROJECT_CODE_RE = re.compile(r"-pj(\d+)(?:[/?#]|$)")
PROJECT_CODE_TAIL_RE = re.compile(r"-pj\d+$")
GENERATED_SOURCES = ["homedy", "nhadatcanban"]  # nguồn cần sinh slug từ tên dự án


# ---------- hàm tiện ích ----------
def safe_text(el):
    return el.text.strip() if el else None


def get_number(text):
    if not text:
        return None
    text = str(text).replace(".", "").replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if match:
        return float(match.group(1))
    return None


def parse_land_area(text):
    if not text:
        return None
    text = text.lower().strip()
    num = get_number(text)
    if num:
        if "ha" in text:
            return num
        elif "m²" in text or "m2" in text:
            return num / 10000
    return num


def extract_project_code(url):
    """.../de-la-sol-pj3464 -> 'pj3464'. Không có mã thì trả None."""
    match = PROJECT_CODE_RE.search(url or "")
    return f"pj{match.group(1)}" if match else None

def extract_slug(url):
    """.../quan-4/de-la-sol-pj3464 -> 'de-la-sol' (bỏ mã pj, mã vẫn lưu riêng ở source_project_code)."""
    path = (url or "").split("?")[0].split("#")[0].rstrip("/")
    last = path.rsplit("/", 1)[-1]
    return PROJECT_CODE_TAIL_RE.sub("", last) or None


# ---------- trình duyệt ----------
def connect_chrome():
    opts = Options()
    opts.add_experimental_option("debuggerAddress", CHROME_DEBUG_ADDR)
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as exc:
        raise SystemExit(
            "Không nối được Chrome. Đóng hết Chrome, mở lại bằng lệnh sau rồi chạy lại:\n"
            '  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
            "--remote-debugging-port=9222 --user-data-dir=C:\\chrome-debug\n"
            f"Chi tiết: {str(exc)[:120]}"
        )
    print("Connected to Chrome")
    return driver


# ---------- Bước 1: danh sách URL ----------
def get_projects_from_page(driver):
    projects = []
    cards = driver.find_elements(By.CSS_SELECTOR, ".re__prj-card-full")
    print(f"   Found {len(cards)} project cards")

    for card in cards:
        try:
            link_elem = card.find_element(By.CSS_SELECTOR, "a")
            url = link_elem.get_attribute("href")
            if not url or "batdongsan.com.vn" not in url:
                continue

            projects.append({
                "url": url,
                "title": card.find_element(By.CSS_SELECTOR, ".re__prj-card-title").text.strip(),
                "status": card.find_element(By.CSS_SELECTOR, ".re__prj-tag-info label").text.strip(),
                "location_raw": card.find_element(By.CSS_SELECTOR, ".re__prj-card-location").text.strip(),
            })
        except Exception:
            continue
    return projects


def iter_project_pages(driver, max_pages=None):
    """Mỗi lần trả về (số trang, danh sách dự án mới của trang đó)."""
    seen = set()
    page = 1
    while True:
        url = f"{BASE_URL}{QUERY_PARAMS}" if page == 1 else f"{BASE_URL}/p{page}{QUERY_PARAMS}"
        print(f"\nPage {page}: {url}")

        driver.get(url)
        time.sleep(random.uniform(3, 5))
        driver.execute_script("window.scrollBy(0, 500);")
        time.sleep(1)

        projects = get_projects_from_page(driver)
        fresh = [p for p in projects if p["url"] not in seen]
        seen.update(p["url"] for p in fresh)
        print(f"   Found: {len(projects)} projects, New: {len(fresh)}, Total: {len(seen)}")

        if not fresh:
            print("   No new projects found. Stopping.")
            return
        yield page, fresh

        if max_pages and page >= max_pages:
            print(f"   Reached max pages limit ({max_pages}). Stopping.")
            return
        page += 1


def run_urls(driver, db, max_pages=None):
    print("\n" + "=" * 60)
    print("BƯỚC 1: CRAWL URL DỰ ÁN -> crawl.project_urls")
    print("=" * 60)

    total_new = total_seen = total_skipped = 0
    for _page, projects in iter_project_pages(driver, max_pages):
        for p in projects:
            code = extract_project_code(p["url"])
            if not code:
                db.log_error("project_urls", SOURCE_NAME, p["url"], "no_code",
                             "URL không có mã pj...")
                total_skipped += 1
                continue
            _project_id, is_new = db.upsert_project_url(
                SOURCE_NAME, code, p["url"], p["title"], p["status"], p["location_raw"],
                extract_slug(p["url"]),
            )
            total_seen += 1
            total_new += 1 if is_new else 0

    print(f"\nXong bước 1: {total_seen} URL ({total_new} mới, {total_seen - total_new} đã có), "
          f"bỏ qua {total_skipped} URL không có mã pj")


# ---------- Bước 2: chi tiết dự án ----------
def parse_project_html(soup, info):
    """Bóc dữ liệu từ HTML trang chi tiết. Trả về dict khớp core.db.PROJECT_COLUMNS."""
    project_name = safe_text(soup.select_one(".re__project-name")) or info["title"]
    address_raw = safe_text(soup.select_one(".re__project-address")) or info["location_raw"]
    description = safe_text(soup.select_one(".js__prj-detail-content"))
    project_type = safe_text(soup.select_one(".re__prj-cat span"))
    project_status = safe_text(soup.select_one(".re__prj-tag-info label")) or info["status_raw"]
    investor = safe_text(soup.select_one(".re__prj-investor a"))

    # Tọa độ
    longitude = latitude = None
    map_div = soup.select_one("#data-album-google-map")
    if map_div:
        src = map_div.get("data-src", "")
        match = re.search(r"q=([-\d.]+),([-\d.]+)", src)
        if match:
            latitude = float(match.group(1))
            longitude = float(match.group(2))

    # 3 thông số đầu trang
    land_area_ha = None
    total_apartment = None
    for wrapper in soup.select(".re__project-info-details .re__project-info-details__wrapper"):
        icon_elem = wrapper.select_one(".re__project-info-details__icon")
        if not icon_elem:
            continue
        icon_class = " ".join(icon_elem.get("class", []))
        if "money" in icon_class:
            continue

        value = safe_text(wrapper.select_one(".re__project-info-details__value"))
        unit = safe_text(wrapper.select_one(".re__project-info-details__unit"))
        if "house" in icon_class:
            total_apartment = get_number(value)
        elif "building" in icon_class:
            land_area_ha = parse_land_area(f"{value} {unit}" if unit else value)

    # Thông tin chi tiết
    num_towers = construction_density = launch_year = handover_year = ownership = None
    for item in soup.select(".re__project-box-item"):
        label = safe_text(item.select_one("label"))
        value = safe_text(item.select_one("span"))
        if not label or not value:
            continue

        label_lower = label.lower()
        if "diện tích" in label_lower and not land_area_ha:
            land_area_ha = parse_land_area(value)
        elif "số căn hộ" in label_lower and not total_apartment:
            total_apartment = get_number(value)
        elif "số tòa" in label_lower or "quy mô" in label_lower:
            num_towers = get_number(value)
        elif "mật độ" in label_lower:
            construction_density = get_number(value)
        elif "khởi công" in label_lower:
            launch_year = get_number(value)
        elif "bàn giao" in label_lower or "hoàn thành" in label_lower:
            handover_year = get_number(value)
        elif "pháp lý" in label_lower or "sở hữu" in label_lower:
            ownership = value

    # Tiện ích
    amenities = [safe_text(li).lower() for li in soup.select(".re__prj-facilities ul li") if safe_text(li)]

    def has(*keywords):
        return 1 if any(k in a for a in amenities for k in keywords) else 0

    return {
        "project_name": project_name,
        "project_type": project_type,
        "project_status": project_status,
        "investor": investor,
        "address_raw": address_raw,
        "longitude": longitude,
        "latitude": latitude,
        "land_area_ha": land_area_ha,
        "num_towers": num_towers,
        "total_apartment": total_apartment,
        "construction_density": construction_density,
        "has_mall": has("trung tâm thương mại", "siêu thị"),
        "has_school": has("trường"),
        "has_hospital": has("bệnh viện", "trạm y tế"),
        "has_park": has("công viên"),
        "has_pool": has("hồ bơi"),
        "has_parkinglot": has("bãi đỗ", "bãi giữ"),
        "launch_year": launch_year,
        "handover_year": handover_year,
        "ownership": ownership,
        "project_description": description,
        "source_url": info["url"],
    }


def crawl_project_detail(driver, info):
    print(f"   Accessing: {info['url']}")
    driver.get(info["url"])
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".project-main-info"))
    )
    time.sleep(2)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    return parse_project_html(soup, info)


def run_details(driver, db, limit=None, max_attempts=3):
    print("\n" + "=" * 60)
    print("BƯỚC 2: CRAWL CHI TIẾT DỰ ÁN -> crawl.projects")
    print("=" * 60)

    todo = db.get_pending_projects(limit=limit, max_attempts=max_attempts)
    print(f"{len(todo)} dự án cần crawl chi tiết")

    ok = failed = consecutive_fails = 0
    for i, info in enumerate(todo, 1):
        print(f"\n[{i}/{len(todo)}] {info['title']} ({info['project_id']})")
        try:
            data = crawl_project_detail(driver, info)
            db.save_project(info["project_id"], data, PARSER_VERSION)
            ok += 1
            consecutive_fails = 0
            land = f"{data['land_area_ha']:.4f} ha" if data["land_area_ha"] else "N/A"
            apt = int(data["total_apartment"]) if data["total_apartment"] else "N/A"
            print(f"   OK | Land: {land} | Apartments: {apt}")
        except Exception as exc:
            db.mark_project_failed(info["project_id"], info["url"],
                                   f"{type(exc).__name__}: {exc}", SOURCE_NAME)
            failed += 1
            consecutive_fails += 1
            print(f"   FAILED: {str(exc)[:100]}")
            if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
                print(f"\n{MAX_CONSECUTIVE_FAILS} lỗi liên tiếp, có thể bị chặn (Cloudflare). "
                      "Mở Chrome, vào batdongsan giải captcha rồi chạy lại.")
                break

        time.sleep(random.uniform(1.5, 3))

    print(f"\nXong bước 2: {ok} thành công, {failed} lỗi")


# ---------- Bước 3: sinh slug cho homedy / nhadatcanban (không cần trình duyệt) ----------
def run_slugs(db):
    print("\n" + "=" * 60)
    print("BƯỚC 3: SINH SLUG CHO homedy / nhadatcanban -> crawl.project_sources")
    print("=" * 60)

    projects = db.get_projects_for_slugs()
    rows = []
    for p in projects:
        slug = slugify(p["name"])
        if not slug:
            db.log_error("project_slugs", None, None, "no_slug",
                         f"Không sinh được slug từ tên: {p['name']!r}", ref_id=p["project_id"])
            continue
        rows.extend((p["project_id"], source, slug) for source in GENERATED_SOURCES)

    created = db.add_generated_slugs(rows)
    print(f"Xong bước 3: {len(projects)} dự án, thêm {created} dòng slug mới "
          f"({len(rows) - created} dòng đã có, giữ nguyên)")


# ---------- main ----------
def main():
    parser = argparse.ArgumentParser(description="Crawl dự án batdongsan -> Supabase")
    parser.add_argument("stage", nargs="?", default="all",
                        choices=["all", "urls", "details", "slugs"])
    parser.add_argument("--max-pages", type=int, default=None, help="Giới hạn số trang (bước 1)")
    parser.add_argument("--limit", type=int, default=None, help="Giới hạn số dự án (bước 2)")
    parser.add_argument("--max-attempts", type=int, default=3, help="Số lần thử tối đa mỗi dự án (bước 2)")
    args = parser.parse_args()

    with Db() as db:
        name, now = db.ping()
        print(f"Đã nối DB: {name} ({now:%Y-%m-%d %H:%M:%S})")

        driver = None
        if args.stage in ("all", "urls", "details"):
            driver = connect_chrome()  # không quit để giữ Chrome đang mở

        if args.stage in ("all", "urls"):
            run_urls(driver, db, args.max_pages)
        if args.stage in ("all", "details"):
            run_details(driver, db, args.limit, args.max_attempts)
        if args.stage in ("all", "slugs"):
            run_slugs(db)

        print(f"\nTiến độ: {db.progress()}")


if __name__ == "__main__":
    main()