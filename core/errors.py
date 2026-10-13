class CrawlError(Exception):
    """Lớp cha của mọi lỗi crawl."""


class BlockedError(CrawlError):
    """Bị chặn (Cloudflare/captcha/HTTP 403, 429). Cần chờ hoặc giải captcha rồi chạy lại."""


class NotFoundError(CrawlError):
    """Trang không tồn tại (HTTP 404 hoặc bị chuyển về trang lỗi)."""


class ListingGone(NotFoundError):
    """Tin đã bị gỡ / không còn tồn tại."""


class FetchError(CrawlError):
    """Lỗi mạng hoặc HTTP 5xx: chỉ là tạm thời, nên thử lại sau.
    KHÔNG được kết luận 'không tồn tại' chỉ vì gặp lỗi này."""