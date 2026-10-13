# TODO: logger dùng chung
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("logs")


def setup_logging(prefix: str) -> Path:
    """Ghi log ra màn hình và vào logs/<prefix>_<tên máy>_<YYYYMMDD>.log."""
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / f"{prefix}_{socket.gethostname()}_{datetime.now():%Y%m%d}.log"
    root = logging.getLogger()
    if not root.handlers:
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
        for handler in (logging.StreamHandler(sys.stdout),
                        logging.FileHandler(log_path, encoding="utf-8")):
            handler.setFormatter(fmt)
            root.addHandler(handler)
    for noisy in ("selenium", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path