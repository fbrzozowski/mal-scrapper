"""CSV output and access-token scrubbing."""

import csv
import json
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

COLUMNS = [
    "id",
    "page_id",
    "page_name",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "age_country_gender_reach_breakdown",
    "media_count",
    "media_files",
    "media_error",
]

_TOKEN_RE = re.compile(r"(access_token=)[^&\s\"']+")


def redact(text: str) -> str:
    return _TOKEN_RE.sub(r"\1REDACTED", text)


def strip_token(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "access_token"]
    return urlunsplit(parts._replace(query=urlencode(query)))


def to_row(ad: dict, media_files: list[str], media_error: str = "") -> dict:
    breakdown = ad.get("age_country_gender_reach_breakdown")
    return {
        "id": ad.get("id", ""),
        "page_id": ad.get("page_id", ""),
        "page_name": ad.get("page_name", ""),
        "ad_delivery_start_time": ad.get("ad_delivery_start_time", ""),
        "ad_delivery_stop_time": ad.get("ad_delivery_stop_time", ""),
        "ad_snapshot_url": strip_token(ad["ad_snapshot_url"]) if ad.get("ad_snapshot_url") else "",
        "age_country_gender_reach_breakdown": json.dumps(breakdown, ensure_ascii=False) if breakdown else "",
        "media_count": len(media_files),
        "media_files": ";".join(media_files),
        "media_error": media_error,
    }


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def append_row(path: Path, row: dict) -> None:
    """Append one ad right away so an interrupted run keeps what it already fetched."""
    new = not path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def dedupe(path: Path) -> int:
    """Keep the last row per ad id (resumed runs re-append retried ads). Returns row count."""
    by_id = {r["id"]: r for r in read_rows(path)}
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(by_id.values())
    return len(by_id)
