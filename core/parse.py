# TODO: parse giá / diện tích / ngày dùng chung
import re
import unicodedata


def slugify(text):
    """'Khải Hoàn Prime' -> 'khai-hoan-prime'. Dùng chung để sinh slug cho các nguồn."""
    if not text:
        return None
    text = str(text).replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or None