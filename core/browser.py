"""Trình duyệt dùng chung cho mọi crawler dùng Selenium (projects, listings...).

Cần mở sẵn Chrome ở chế độ debug (xem CHROME_START_CMD).
"""
import logging
import random
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

log = logging.getLogger("core.browser")

CHROME_DEBUG_ADDR = "127.0.0.1:9222"
CHROME_START_CMD = (
    '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
    "--remote-debugging-port=9222 --user-data-dir=C:\\chrome-debug"
)

# Dấu hiệu trang chặn của Cloudflare / captcha (so khớp trên tiêu đề trang)
BLOCK_TITLE_MARKERS = ("just a moment", "attention required", "access denied", "verify you are human")


def connect_chrome(addr: str = CHROME_DEBUG_ADDR):
    opts = Options()
    opts.add_experimental_option("debuggerAddress", addr)
    try:
        driver = webdriver.Chrome(options=opts)
    except Exception as exc:
        raise SystemExit(
            "Không nối được Chrome. Đóng hết Chrome, mở lại bằng lệnh sau rồi chạy lại:\n"
            f"  {CHROME_START_CMD}\n"
            f"Chi tiết: {str(exc)[:120]}"
        )
    log.info("Đã nối Chrome (%s)", addr)
    return driver


def looks_blocked(driver) -> bool:
    """True nếu trang hiện tại giống màn hình chặn bot."""
    try:
        title = (driver.title or "").lower()
    except Exception:
        return False
    return any(marker in title for marker in BLOCK_TITLE_MARKERS)


def sleep_between(delay_range) -> None:
    """Ngủ ngẫu nhiên trong khoảng (min, max) giây."""
    time.sleep(random.uniform(*delay_range))