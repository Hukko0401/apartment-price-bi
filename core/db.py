"""Kết nối Supabase (PostgreSQL) dùng chung cho mọi crawler.

Crawler KHÔNG tự viết SQL: chỉ gọi các hàm trong file này.
"""
import os

import psycopg2
from psycopg2.extras import execute_values
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

ENV_KEY = "SUPABASE_DB_URL"

PROJECT_COLUMNS = [
    "project_name", "project_type", "project_status", "investor", "address_raw",
    "longitude", "latitude", "land_area_ha", "num_towers", "total_apartment",
    "construction_density", "has_mall", "has_school", "has_hospital", "has_park",
    "has_pool", "has_parkinglot", "launch_year", "handover_year", "ownership",
    "project_description", "source_url",
]
INT_COLUMNS = {"num_towers", "total_apartment", "launch_year", "handover_year", "has_mall", "has_school", "has_hospital", "has_park", "has_pool", "has_parkinglot"}

LISTING_COLUMNS = [
    "title", "description_text", "price_raw", "price_total_vnd", "area_raw", "area_m2",
    "bedrooms", "bathrooms", "direction", "balcony_direction", "legal_status",
    "furniture_status", "project_name_raw", "address_raw", "address_old_raw",
    "posted_at", "expired_at", "contact_name", "contact_phone_masked",
]


def _to_int(value):
    """Số bóc từ web hay ra dạng float (3.0) -> int. Không đọc được -> None."""
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


class Db:
    def __init__(self):
        self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------- kết nối ----------
    def _get(self):
        if self._conn is None or self._conn.closed:
            url = os.getenv(ENV_KEY)
            if not url:
                raise RuntimeError(
                    f"Thiếu {ENV_KEY}. Copy .env.example thành .env rồi điền chuỗi kết nối Supabase."
                )
            self._conn = psycopg2.connect(url, connect_timeout=15, sslmode=os.getenv("DB_SSLMODE", "require"))
        return self._conn

    def close(self):
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def _run(self, fn):
        """Chạy fn(cursor) trong 1 transaction. Mất kết nối thì nối lại và thử thêm 1 lần."""
        for attempt in (1, 2):
            try:
                with self._get() as conn:
                    with conn.cursor() as cur:
                        return fn(cur)
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self.close()
                if attempt == 2:
                    raise

    def ping(self):
        def fn(cur):
            cur.execute("select current_database(), now()")
            return cur.fetchone()
        return self._run(fn)

    # ---------- Bước 1: project_urls ----------
    def upsert_project_url(self, source_name, source_project_code, url,
                           title=None, status_raw=None, location_raw=None, slug=None):
        """Thêm URL dự án (+ dòng nguồn tương ứng trong project_sources).
        Đã có thì cập nhật thông tin hiển thị, GIỮ NGUYÊN project_id.
        Trả về (project_id, is_new). Chạy lại nhiều lần không làm nhảy số project_id."""
        update_sql = """
            update crawl.project_urls
            set url = %s, title = %s, status_raw = %s, location_raw = %s
            where source_name = %s and source_project_code = %s
            returning project_id
        """
        insert_sql = """
            insert into crawl.project_urls
                (source_name, source_project_code, url, title, status_raw, location_raw)
            values (%s, %s, %s, %s, %s, %s)
            on conflict (source_name, source_project_code) do update set
                url = excluded.url,
                title = excluded.title,
                status_raw = excluded.status_raw,
                location_raw = excluded.location_raw
            returning project_id, (xmax = 0) as is_new
        """
        source_sql = """
            insert into crawl.project_sources (project_id, source_name, slug, url, slug_origin)
            values (%s, %s, %s, %s, 'from_url')
            on conflict (project_id, source_name) do update set
                slug = excluded.slug,
                url = excluded.url
        """

        def fn(cur):
            cur.execute(update_sql, (url, title, status_raw, location_raw, source_name, source_project_code))
            row = cur.fetchone()
            if row:
                result = (row[0], False)
            else:
                cur.execute(insert_sql, (source_name, source_project_code, url, title, status_raw, location_raw))
                result = cur.fetchone()
            cur.execute(source_sql, (result[0], source_name, slug, url))
            return result
        return self._run(fn)

    # ---------- project_sources: slug theo từng nguồn ----------
    def get_projects_for_slugs(self):
        """Mỗi dự án 1 dòng: tên tốt nhất hiện có (tên ở trang chi tiết, nếu chưa có thì tên ở danh sách)."""
        def fn(cur):
            cur.execute(
                """select u.project_id, coalesce(p.project_name, u.title) as name
                   from crawl.project_urls u
                   left join crawl.projects p on p.project_id = u.project_id
                   order by u.project_id"""
            )
            return [{"project_id": r[0], "name": r[1]} for r in cur.fetchall()]
        return self._run(fn)

    def add_generated_slugs(self, rows):
        """rows = [(project_id, source_name, slug), ...]. Dòng đã có thì giữ nguyên
        (không ghi đè slug đã sửa tay). Trả về số dòng mới."""
        if not rows:
            return 0

        def fn(cur):
            cur.execute("select count(*) from crawl.project_sources")
            before = cur.fetchone()[0]
            execute_values(
                cur,
                """insert into crawl.project_sources (project_id, source_name, slug, slug_origin)
                   values %s
                   on conflict (project_id, source_name) do nothing""",
                [(pid, source, slug, "generated") for pid, source, slug in rows],
            )
            cur.execute("select count(*) from crawl.project_sources")
            return cur.fetchone()[0] - before
        return self._run(fn)

    # ---------- Bước 2: projects ----------
    def get_pending_projects(self, limit=None, max_attempts=3):
        """Dự án chưa crawl chi tiết (pending) hoặc từng lỗi nhưng chưa quá số lần thử."""
        sql = """
            select project_id, url, title, status_raw, location_raw
            from crawl.project_urls
            where detail_status in ('pending', 'failed') and detail_attempts < %s
            order by project_id
        """
        params = [max_attempts]
        if limit:
            sql += " limit %s"
            params.append(limit)

        def fn(cur):
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        return self._run(fn)

    def save_project(self, project_id, data, parser_version):
        """Ghi master data của dự án và đánh dấu project_urls là 'done' (cùng 1 transaction)."""
        values = [
            _to_int(data.get(col)) if col in INT_COLUMNS else data.get(col)
            for col in PROJECT_COLUMNS
        ]
        cols = ", ".join(PROJECT_COLUMNS)
        marks = ", ".join(["%s"] * len(PROJECT_COLUMNS))
        updates = ", ".join(f"{c} = excluded.{c}" for c in PROJECT_COLUMNS)
        upsert = f"""
            insert into crawl.projects (project_id, {cols}, parser_version, scraped_at)
            values (%s, {marks}, %s, now())
            on conflict (project_id) do update set
                {updates},
                parser_version = excluded.parser_version,
                scraped_at = now()
        """
        done = """
            update crawl.project_urls
            set detail_status = 'done', detail_done_at = now(), last_error = null
            where project_id = %s
        """

        def fn(cur):
            cur.execute(upsert, [project_id, *values, parser_version])
            cur.execute(done, (project_id,))
        self._run(fn)

    def mark_project_failed(self, project_id, url, message, source_name="batdongsan",
                            error_type="failed"):
        msg = (message or "")[:500]

        def fn(cur):
            cur.execute(
                """update crawl.project_urls
                   set detail_status = 'failed', detail_attempts = detail_attempts + 1, last_error = %s
                   where project_id = %s""",
                (msg, project_id),
            )
            cur.execute(
                """insert into crawl.crawl_errors (stage, source_name, ref_id, url, error_type, message)
                   values ('project_detail', %s, %s, %s, %s, %s)""",
                (source_name, project_id, url, error_type, msg),
            )
        self._run(fn)

    # ---------- chung ----------
    def log_error(self, stage, source_name, url, error_type, message, ref_id=None):
        def fn(cur):
            cur.execute(
                """insert into crawl.crawl_errors (stage, source_name, ref_id, url, error_type, message)
                   values (%s, %s, %s, %s, %s, %s)""",
                (stage, source_name, ref_id, url, error_type, (message or "")[:500]),
            )
        self._run(fn)

    def progress(self):
        def fn(cur):
            cur.execute("select detail_status, total from crawl.v_project_progress")
            return {status: total for status, total in cur.fetchall()}
        return self._run(fn)


    # ---------- Hàng chờ tin đăng: mỗi việc = 1 dự án trên 1 nguồn ----------
    def claim_source_job(self, source_name, worker, max_attempts=3, project_id=None):
        sql = """
            with picked as (
                select project_id, source_name
                from crawl.project_sources
                where source_name = %s
                  and crawl_status in ('pending', 'failed')
                  and crawl_attempts < %s
                  and (%s::text is null or project_id = %s)
                order by case crawl_status when 'pending' then 0 else 1 end, project_id
                limit 1
                for update skip locked
            ), upd as (
                update crawl.project_sources s
                set crawl_status = 'running',
                    worker = %s,
                    started_at = now(),
                    finished_at = null,
                    crawl_attempts = s.crawl_attempts + 1
                from picked
                where s.project_id = picked.project_id and s.source_name = picked.source_name
                returning s.project_id, s.source_name, s.slug, s.url, s.listing_list_url
            )
            select upd.project_id, upd.source_name, upd.slug, upd.url, upd.listing_list_url,
                   coalesce(p.project_name, u.title) as project_name
            from upd
            join crawl.project_urls u on u.project_id = upd.project_id
            left join crawl.projects p on p.project_id = upd.project_id
        """

        def fn(cur):
            cur.execute(sql, (source_name, max_attempts, project_id, project_id, worker))
            row = cur.fetchone()
            if not row:
                return None
            cols = [c[0] for c in cur.description]
            return dict(zip(cols, row))
        return self._run(fn)

    def finish_source_job(self, project_id, source_name, listings_found, listing_list_url=None):
        def fn(cur):
            cur.execute(
                """update crawl.project_sources
                   set crawl_status = 'done', listings_found = %s, finished_at = now(),
                       last_error = null, listing_list_url = coalesce(%s, listing_list_url)
                   where project_id = %s and source_name = %s""",
                (listings_found, listing_list_url, project_id, source_name),
            )
        self._run(fn)

    def fail_source_job(self, project_id, source_name, message, error_type="failed"):
        msg = (message or "")[:500]

        def fn(cur):
            cur.execute(
                """update crawl.project_sources
                   set crawl_status = 'failed', finished_at = now(), last_error = %s
                   where project_id = %s and source_name = %s""",
                (msg, project_id, source_name),
            )
            cur.execute(
                """insert into crawl.crawl_errors (stage, source_name, ref_id, error_type, message)
                   values ('source_job', %s, %s, %s, %s)""",
                (source_name, project_id, error_type, msg),
            )
        self._run(fn)

    def mark_source_not_found(self, project_id, source_name):
        """Đã tìm nhưng trang đó không có dự án này (không phải lỗi, không cần thử lại)."""
        def fn(cur):
            cur.execute(
                """update crawl.project_sources
                   set crawl_status = 'not_found', listings_found = 0, finished_at = now(), last_error = null
                   where project_id = %s and source_name = %s""",
                (project_id, source_name),
            )
        self._run(fn)


    def update_source_url(self, project_id, source_name, url):
        """Crawler tìm được link thật của dự án trên trang đó thì lưu lại."""
        def fn(cur):
            cur.execute(
                "update crawl.project_sources set url = %s where project_id = %s and source_name = %s",
                (url, project_id, source_name),
            )
        self._run(fn)

    def set_listing_list_url(self, project_id, source_name, list_url):
        """Lưu ngay link tổng hợp tin vừa dò được, để lần chạy lại không phải dò lại."""
        def fn(cur):
            cur.execute(
                "update crawl.project_sources set listing_list_url = %s "
                "where project_id = %s and source_name = %s",
                (list_url, project_id, source_name),
            )
        self._run(fn)

    def release_stale_jobs(self, source_name=None, minutes=60):
        """Việc 'running' quá lâu (máy tắt giữa chừng) -> trả về 'pending' để máy khác làm.
        Trả về số việc được trả lại."""
        def fn(cur):
            cur.execute(
                """update crawl.project_sources
                   set crawl_status = 'pending', worker = null
                   where crawl_status = 'running'
                     and started_at < now() - make_interval(mins => %s)
                     and (%s::text is null or source_name = %s)""",
                (minutes, source_name, source_name),
            )
            return cur.rowcount
        return self._run(fn)

    def source_progress(self):
        """{'homedy': {'pending': 300, 'done': 17}, ...}"""
        def fn(cur):
            cur.execute("select source_name, crawl_status, total from crawl.v_source_progress")
            result = {}
            for source, status, total in cur.fetchall():
                result.setdefault(source, {})[status] = total
            return result
        return self._run(fn)

    # ---------- Tin đăng ----------
    def save_listing_urls(self, cards, all_types=False):
        """Upsert card của 1 trang list. Trả về số tin MỚI. Tin không phải căn hộ chung cư
        được lưu với trạng thái 'skipped' (trừ khi all_types)."""
        sql = """
            insert into crawl.listing_urls as lu
                (source_name, source_listing_id, listing_url, project_id, category_slug,
                 is_apartment, list_page_url, page_number, vip_type, card_title,
                 card_price_raw, card_area_raw, card_location_raw, card_posted_raw, detail_status)
            values
                (%(source_name)s, %(source_listing_id)s, %(listing_url)s, %(project_id)s,
                 %(category_slug)s, %(is_apartment)s, %(list_page_url)s, %(page_number)s,
                 %(vip_type)s, %(card_title)s, %(card_price_raw)s, %(card_area_raw)s,
                 %(card_location_raw)s, %(card_posted_raw)s, %(detail_status)s)
            on conflict (source_name, source_listing_id) do update set
                last_seen_at      = now(),
                listing_url       = excluded.listing_url,
                project_id        = coalesce(lu.project_id, excluded.project_id),
                vip_type          = excluded.vip_type,
                card_title        = excluded.card_title,
                card_price_raw    = excluded.card_price_raw,
                card_area_raw     = excluded.card_area_raw,
                card_location_raw = excluded.card_location_raw,
                card_posted_raw   = excluded.card_posted_raw
            returning (xmax = 0) as inserted
        """

        def fn(cur):
            new_count = 0
            for card in cards:
                status = "pending" if (card["is_apartment"] or all_types) else "skipped"
                cur.execute(sql, {**card, "detail_status": status})
                new_count += 1 if cur.fetchone()[0] else 0
            return new_count
        return self._run(fn)

    def fetch_pending_listings(self, source_name, limit=None, max_attempts=3):
        def fn(cur):
            cur.execute(
                """select source_listing_id, listing_url, project_id,
                          card_title, card_price_raw, card_area_raw
                   from crawl.listing_urls
                   where source_name = %s
                     and detail_status in ('pending', 'failed')
                     and detail_attempts < %s
                   order by (detail_status = 'failed'), discovered_at
                   limit %s""",
                (source_name, max_attempts, limit or 1_000_000),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        return self._run(fn)

    def save_listing(self, source_name, source_listing_id, project_id, listing_url, data, parser_version):
        """Ghi chi tiết tin và đánh dấu listing_urls là 'done' (cùng 1 transaction)."""
        params = {col: data.get(col) for col in LISTING_COLUMNS}
        params.update({"src": source_name, "lid": source_listing_id, "pid": project_id,
                       "url": listing_url, "ver": parser_version})
        cols = ", ".join(LISTING_COLUMNS)
        marks = ", ".join(f"%({c})s" for c in LISTING_COLUMNS)
        updates = ", ".join(f"{c} = excluded.{c}" for c in LISTING_COLUMNS)
        upsert = f"""
            insert into crawl.listings
                (source_name, source_listing_id, project_id, listing_url, parser_version, {cols})
            values (%(src)s, %(lid)s, %(pid)s, %(url)s, %(ver)s, {marks})
            on conflict (source_name, source_listing_id) do update set
                project_id = coalesce(crawl.listings.project_id, excluded.project_id),
                listing_url = excluded.listing_url,
                parser_version = excluded.parser_version,
                scraped_at = now(),
                {updates}
        """
        done = """
            update crawl.listing_urls
            set detail_status = 'done', detail_done_at = now(),
                detail_attempts = detail_attempts + 1, last_error = null
            where source_name = %(src)s and source_listing_id = %(lid)s
        """

        def fn(cur):
            cur.execute(upsert, params)
            cur.execute(done, params)
        self._run(fn)

    def mark_listing_detail(self, source_name, source_listing_id, status, message=None):
        """status: 'failed' (thử lại nếu chưa quá max_attempts) hoặc 'gone' (tin đã bị gỡ)."""
        msg = (message or "")[:500]

        def fn(cur):
            cur.execute(
                """update crawl.listing_urls
                   set detail_status = %s, detail_attempts = detail_attempts + 1, last_error = %s
                   where source_name = %s and source_listing_id = %s""",
                (status, msg, source_name, source_listing_id),
            )
        self._run(fn)

    def listing_progress(self, source_name=None):
        def fn(cur):
            cur.execute(
                """select detail_status, count(*) from crawl.listing_urls
                   where (%s::text is null or source_name = %s)
                   group by detail_status order by detail_status""",
                (source_name, source_name),
            )
            return {status: total for status, total in cur.fetchall()}
        return self._run(fn)
