# TODO: get(url) + wait(domain); thông số retry/delay theo domain
from __future__ import annotations

from typing import Sequence, Tuple

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.errors import BlockedError, FetchError, NotFoundError

DEFAULT_TIMEOUT = 25
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    # Nên cập nhật số phiên bản Chrome này định kỳ cho khớp trình duyệt thật
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
}


def make_session(retries: int = 3, backoff: float = 1.0) -> requests.Session:
    """Session tự thử lại khi 429/5xx. Hết lượt thử thì TRẢ response cuối (không ném lỗi),
    để fetch_html tự phân loại theo mã trạng thái."""
    session = requests.Session()
    retry = Retry(
        total=retries, connect=retries, read=retries, status=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch_html(session: requests.Session, url: str, timeout: int = DEFAULT_TIMEOUT,
               not_found_markers: Sequence[str] = ("/error/404",)) -> Tuple[BeautifulSoup, str]:
    """Tải 1 trang. Trả về (soup, url cuối sau khi redirect).
      BlockedError  - HTTP 403/429: bị chặn, dừng lại.
      NotFoundError - HTTP 404 hoặc bị chuyển về trang lỗi (not_found_markers).
      FetchError    - lỗi mạng, timeout, 5xx: tạm thời, KHÔNG kết luận 'không tồn tại'."""
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    final_url = resp.url
    code = resp.status_code
    if code in (403, 429):
        raise BlockedError(f"HTTP {code}: {url}")
    if code == 404 or any(marker in final_url for marker in not_found_markers):
        raise NotFoundError(final_url)
    if code >= 400:
        raise FetchError(f"HTTP {code}: {url}")
    return BeautifulSoup(resp.text, "html.parser"), final_url