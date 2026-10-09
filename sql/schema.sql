-- Chạy CẢ FILE trong Supabase > SQL Editor.
-- Chạy lại nhiều lần vẫn an toàn, dùng được cho cả DB mới lẫn DB đã có dữ liệu.

create schema if not exists crawl;

create sequence if not exists crawl.project_id_seq;

-- 1. Danh sách dự án (project_id tự sinh ở đây) + hàng chờ crawl chi tiết (chỉ batdongsan)
create table if not exists crawl.project_urls (
    project_id           text primary key
                         default ('DA_' || lpad(nextval('crawl.project_id_seq')::text, 5, '0')),
    source_name          text        not null default 'batdongsan',
    source_project_code  text        not null,
    url                  text        not null,
    title                text,
    status_raw           text,
    location_raw         text,
    discovered_at        timestamptz not null default now(),
    detail_status        text        not null default 'pending'
                         check (detail_status in ('pending', 'done', 'failed')),
    detail_attempts      integer     not null default 0,
    detail_done_at       timestamptz,
    last_error           text,
    unique (source_name, source_project_code)
);

-- 2. Master data dự án (chi tiết, từ batdongsan)
create table if not exists crawl.projects (
    project_id            text primary key references crawl.project_urls (project_id),
    project_name          text,
    project_type          text,
    project_status        text,
    investor              text,
    address_raw           text,
    longitude             double precision,
    latitude              double precision,
    land_area_ha          numeric,
    num_towers            integer,
    total_apartment       integer,
    construction_density  numeric,
    has_mall              smallint,
    has_school            smallint,
    has_hospital          smallint,
    has_park              smallint,
    has_pool              smallint,
    has_parkinglot        smallint,
    launch_year           integer,
    handover_year         integer,
    ownership             text,
    project_description   text,
    source_url            text,
    scraped_at            timestamptz not null default now(),
    parser_version        text
);

-- 3. Nhật ký lỗi chung cho mọi bước crawl
create table if not exists crawl.crawl_errors (
    id           bigserial primary key,
    stage        text        not null,
    source_name  text,
    ref_id       text,
    url          text,
    error_type   text,
    message      text,
    created_at   timestamptz not null default now()
);

-- 4. Mỗi dự án x mỗi nguồn = 1 dòng: slug + hàng chờ crawl tin đăng
create table if not exists crawl.project_sources (
    project_id   text        not null references crawl.project_urls (project_id),
    source_name  text        not null check (source_name in ('batdongsan', 'homedy', 'nhadatcanban')),
    slug         text,
    url          text,
    slug_origin  text        not null default 'generated'
                 check (slug_origin in ('from_url', 'generated', 'manual')),
    created_at   timestamptz not null default now(),
    primary key (project_id, source_name)
);

-- Cột hàng chờ (DB đã có bảng thì chỉ bổ sung cột còn thiếu)
alter table crawl.project_sources
    add column if not exists crawl_status   text not null default 'pending'
        check (crawl_status in ('pending', 'running', 'done', 'failed', 'not_found')),
    add column if not exists crawl_attempts integer not null default 0,
    add column if not exists worker         text,
    add column if not exists started_at     timestamptz,
    add column if not exists finished_at    timestamptz,
    add column if not exists listings_found integer,
    add column if not exists last_error     text;

create index if not exists project_sources_source_slug_idx
    on crawl.project_sources (source_name, slug);

create index if not exists project_sources_queue_idx
    on crawl.project_sources (source_name, crawl_status);

-- Bù dòng batdongsan cho các dự án đã crawl trước đó
insert into crawl.project_sources (project_id, source_name, slug, url, slug_origin)
select project_id,
       source_name,
       nullif(regexp_replace(regexp_replace(url, '^.*/([^/?#]+).*$', '\1'), '-pj[0-9]+$', ''), ''),
       url,
       'from_url'
from crawl.project_urls
where source_name = 'batdongsan'
on conflict (project_id, source_name) do nothing;

-- Các view để xem tiến độ
create or replace view crawl.v_project_progress as
select detail_status, count(*) as total
from crawl.project_urls
group by detail_status
order by detail_status;

create or replace view crawl.v_source_progress as
select source_name, crawl_status, count(*) as total
from crawl.project_sources
group by source_name, crawl_status
order by source_name, crawl_status;

-- Mỗi dự án 1 dòng, đủ 3 nguồn (để nhìn cho dễ)
create or replace view crawl.v_project_sources_wide as
select u.project_id,
       coalesce(p.project_name, u.title) as project_name,
       max(case when s.source_name = 'batdongsan' then s.slug end)          as slug_batdongsan,
       max(case when s.source_name = 'batdongsan' then s.crawl_status end)  as status_batdongsan,
       max(case when s.source_name = 'homedy' then s.slug end)              as slug_homedy,
       max(case when s.source_name = 'homedy' then s.url end)               as url_homedy,
       max(case when s.source_name = 'homedy' then s.crawl_status end)      as status_homedy,
       max(case when s.source_name = 'nhadatcanban' then s.slug end)        as slug_nhadatcanban,
       max(case when s.source_name = 'nhadatcanban' then s.url end)         as url_nhadatcanban,
       max(case when s.source_name = 'nhadatcanban' then s.crawl_status end) as status_nhadatcanban
from crawl.project_urls u
left join crawl.projects p on p.project_id = u.project_id
left join crawl.project_sources s on s.project_id = u.project_id
group by u.project_id, p.project_name, u.title
order by u.project_id;

-- Bỏ bước sinh slug: slug_origin không còn bắt buộc; homedy/nhadatcanban tự xác định link trong crawler riêng
alter table crawl.project_sources alter column slug_origin drop not null;
alter table crawl.project_sources alter column slug_origin drop default;

-- Xóa slug đã sinh tự động (nếu trước đó có chạy bước slugs). Slug sửa tay (manual) giữ nguyên
update crawl.project_sources
set slug = null, slug_origin = null
where source_name in ('homedy', 'nhadatcanban')
  and slug_origin = 'generated';

-- Tạo sẵn hàng chờ cho 2 nguồn (mỗi dự án 1 dòng/nguồn), chưa có slug. Chạy lại nhiều lần vẫn an toàn
insert into crawl.project_sources (project_id, source_name)
select u.project_id, s.source_name
from crawl.project_urls u
cross join (values ('homedy'), ('nhadatcanban')) as s(source_name)
on conflict (project_id, source_name) do nothing;