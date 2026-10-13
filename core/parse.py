# TODO: parse giá / diện tích / ngày dùng chung
import re
import unicodedata
from datetime import date
from typing import Optional


def slugify(text):
    """'Khải Hoàn Prime' -> 'khai-hoan-prime'. Dùng chung để sinh slug cho các nguồn."""
    if not text:
        return None
    text = str(text).replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or None


def clean_text(text: Optional[str]) -> Optional[str]:
    """Gộp mọi khoảng trắng/xuống dòng về 1 dấu cách. Rỗng -> None."""
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip() or None


def el_text(el) -> Optional[str]:
    """Text sạch của một phần tử BeautifulSoup (None nếu không có phần tử)."""
    return clean_text(el.get_text(" ", strip=True)) if el else None


def parse_number(text: Optional[str]) -> Optional[float]:
    """'3,63' -> 3.63 ; '1.250' -> 1250 (dấu chấm là phân cách nghìn) ; '66,5 m²' -> 66.5"""
    if not text:
        return None
    m = re.search(r"\d[\d.,]*", text)
    if not m:
        return None
    token = m.group(0).rstrip(".,")
    if "." in token and "," in token:
        token = token.replace(".", "").replace(",", ".")
    elif "," in token:
        token = token.replace(",", ".")
    elif "." in token and re.fullmatch(r"\d{1,3}(\.\d{3})+", token):
        token = token.replace(".", "")
    try:
        return float(token)
    except ValueError:
        return None


def parse_int(text: Optional[str]) -> Optional[int]:
    n = parse_number(text)
    return int(round(n)) if n is not None else None


def parse_area_m2(text: Optional[str]) -> Optional[float]:
    n = parse_number(text)
    return round(n, 2) if n is not None and n > 0 else None


def parse_land_area_ha(text: Optional[str]) -> Optional[float]:
    """'12,5 ha' -> 12.5 ; '10.000 m²' -> 1.0 ; không ghi đơn vị thì coi là ha."""
    n = parse_number(text)
    if n is None:
        return None
    t = text.lower()
    if re.search(r"(?<![a-zà-ỹ])ha(?![a-zà-ỹ])", t):
        return n
    if "m²" in t or "m2" in t:
        return n / 10000
    return n


def parse_price_vnd(text: Optional[str]) -> Optional[int]:
    """'3,63 tỷ' -> 3630000000 ; '1 tỷ 500 triệu' -> 1500000000 ;
    'Thỏa thuận' / '55 triệu/m²' -> None (giá/m² không phải giá tổng)."""
    if not text:
        return None
    s = text.lower()
    if "thỏa thuận" in s or "thoả thuận" in s or "liên hệ" in s or "/m" in s:
        return None
    units = {"tỷ": 1_000_000_000, "triệu": 1_000_000, "nghìn": 1_000}
    total = 0.0
    found = False
    for num, unit in re.findall(r"(\d[\d.,]*)\s*(tỷ|triệu|nghìn)", s):
        n = parse_number(num)
        if n is not None:
            total += n * units[unit]
            found = True
    return int(round(total)) if found and total > 0 else None


def parse_date_vn(text: Optional[str]) -> Optional[str]:
    """'02/10/2026' -> '2026-10-02' (None nếu không hợp lệ)."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


def parse_year(text: Optional[str]) -> Optional[int]:
    """'Quý 3/2025' -> 2025 ; '2027' -> 2027 ; None nếu không có năm hợp lệ (1900-2099)."""
    if not text:
        return None
    m = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text)
    return int(m.group(0)) if m else None