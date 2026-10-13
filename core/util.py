import os
import random
import socket
import time


def sleep_between(delay_range) -> None:
    """Ngủ ngẫu nhiên trong khoảng (min, max) giây."""
    time.sleep(random.uniform(*delay_range))


def worker_name() -> str:
    """Tên tiến trình ghi vào cột worker, vd 'LAPTOP-AN-12345'."""
    return f"{socket.gethostname()}-{os.getpid()}"