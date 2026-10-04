"""Extract creative media URLs (images / videos) from an Ad Library ad page.

The page https://www.facebook.com/ads/library/?id=<ad_id> embeds the ad as JSON
("ad_archive_id": "<ad_id>", "snapshot": {...}). The same page also embeds other
ads from that advertiser, so only the snapshot matching the requested id is used.
"""

import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import requests

from meta_ads.config import USER_AGENT

AD_PAGE_URL = "https://www.facebook.com/ads/library/?id={ad_id}"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}

_CHALLENGE_RE = re.compile(r"fetch\('(/__rd_verify[^']+)'")


@dataclass
class MediaItem:
    kind: str  # "image" | "video" | "video_thumb"
    url: str
    quality: str  # "hd" | "sd" | "original" | "resized" | "preview"


def ad_id_from_url(url: str) -> str | None:
    return (parse_qs(urlsplit(url).query).get("id") or [None])[0]


def fetch_ad_page(session: requests.Session, ad_id: str) -> str:
    """GET the ad page, passing Facebook's JS challenge (POST verify, then reload) if served."""
    url = AD_PAGE_URL.format(ad_id=ad_id)
    resp = session.get(url, headers=HEADERS, timeout=60)
    challenge = _CHALLENGE_RE.search(resp.text)
    if challenge:
        session.post("https://www.facebook.com" + challenge.group(1), headers=HEADERS, timeout=60)
        resp = session.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return resp.text


def find_snapshot(page_html: str, ad_id: str) -> dict | None:
    """Return the snapshot dict belonging to ad_id, or None if it isn't on the page."""
    decoder = json.JSONDecoder()
    for m in re.finditer(r'"ad_archive_id":"%s"' % re.escape(ad_id), page_html):
        snap = page_html.find('"snapshot":{', m.end())
        if snap == -1:
            continue
        # The snapshot must belong to this ad, not the next one in the page.
        if page_html.find('"ad_archive_id":"', m.end(), snap) != -1:
            continue
        obj, _ = decoder.raw_decode(page_html, snap + len('"snapshot":'))
        return obj
    return None


def media_from_snapshot(snapshot: dict) -> list[MediaItem]:
    """Collect videos (HD > SD, plus thumbnail) and images (original > resized) incl. carousel cards."""
    entries = []
    for key in ("videos", "extra_videos", "images", "extra_images", "cards"):
        entries += snapshot.get(key) or []

    items = []
    for e in entries:
        if e.get("video_hd_url"):
            items.append(MediaItem("video", e["video_hd_url"], "hd"))
        elif e.get("video_sd_url"):
            items.append(MediaItem("video", e["video_sd_url"], "sd"))
        if e.get("video_preview_image_url"):
            items.append(MediaItem("video_thumb", e["video_preview_image_url"], "preview"))
        if e.get("original_image_url"):
            items.append(MediaItem("image", e["original_image_url"], "original"))
        elif e.get("resized_image_url"):
            items.append(MediaItem("image", e["resized_image_url"], "resized"))
    return dedupe(items)


def dedupe(items: list[MediaItem]) -> list[MediaItem]:
    """fbcdn serves the same file under different signed query strings; dedupe by path."""
    seen: set[str] = set()
    out = []
    for item in items:
        path = urlsplit(item.url).path
        if path not in seen:
            seen.add(path)
            out.append(item)
    return out


def extract_media(session: requests.Session, ad_id: str) -> list[MediaItem]:
    page_html = fetch_ad_page(session, ad_id)
    snapshot = find_snapshot(page_html, ad_id)
    if snapshot is None:
        if '"xfb_ad_library_is_captcha_required":true' in page_html:
            raise RuntimeError("Facebook requires a captcha; slow down (--delay) and retry later")
        raise RuntimeError("ad not found on Ad Library page (removed, or blocked by a login wall)")
    return media_from_snapshot(snapshot)
