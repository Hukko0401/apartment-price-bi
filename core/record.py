from datetime import datetime, timezone

FIELDS = [
    "source_name", "source_listing_id", "listing_url", "title", "description",
    "transaction_type", "listing_tier",
    "price_raw", "price_total_vnd", "area_raw", "area_m2",
    "bedrooms", "bathrooms", "legal_status", "furniture_status", "direction",
    "contact_name", "contact_phone_masked",
    "posted_at_raw", "posted_at", "expired_at_raw", "expired_at",
    "project_name_raw", "source_project_slug", "source_project_url", "location_raw",
    "scraped_at", "parser_version", "discovery",
]


def new_record(source_name: str, parser_version: str, **fields) -> dict:
    """Tạo 1 tin đăng đủ key cố định. Không có -> None, chuỗi rỗng -> None."""
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise KeyError(f"Key không có trong brief: {sorted(unknown)}")

    rec = {k: None for k in FIELDS}
    rec.update(fields)
    for k, v in rec.items():
        if isinstance(v, str) and not v.strip():
            rec[k] = None

    rec["source_name"] = source_name
    rec["parser_version"] = parser_version
    rec["transaction_type"] = rec["transaction_type"] or "sale"
    rec["scraped_at"] = datetime.now(timezone.utc).isoformat()
    return rec