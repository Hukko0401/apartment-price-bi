# Apartment Price BI

Crawl giá chung cư đang mở bán toàn VN (batdongsan, homedy, nhadatcanban) -> PostgreSQL (Supabase).

## Setup
python -m venv .venv
.venv\Scripts\activate      # Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env      # điền key thật vào .env

## Chạy 1 crawler độc lập
python -m crawlers.crawl_<nguon> --keyword "..." --project-id ... --max-pages N

## Quy ước
- Mỗi crawler: SOURCE_NAME, PARSER_VERSION, fetch_listings(job) -> Iterator[dict]
- Tạo tin đăng bằng core.record.new_record(), không tự dựng dict
- Gọi web qua core.http, parse giá/diện tích/ngày qua core.parse
- Không viết SQL trong crawler, chỉ dùng core/db.py
- Sửa logic bóc dữ liệu thì đổi PARSER_VERSION
- Mỗi người một branch, không push thẳng main, merge qua Pull Request
- core/* chỉ người phụ trách feat/db sửa, cần gì thì nhờ