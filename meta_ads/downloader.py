"""Streamed, resumable media downloads."""

import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import requests

from meta_ads.config import USER_AGENT

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


def existing_file(dest_base: Path) -> Path | None:
    for p in dest_base.parent.glob(dest_base.name + ".*"):
        if p.suffix != ".part":
            return p
    return None


def _ext_for(content_type: str, url: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    if ct in CONTENT_TYPE_EXT:
        return CONTENT_TYPE_EXT[ct]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return mimetypes.guess_extension(ct) or ".bin"


def download(session: requests.Session, url: str, dest_base: Path) -> tuple[Path, int]:
    """Download url to dest_base + extension. Skips if a finished file already exists."""
    if found := existing_file(dest_base):
        return found, found.stat().st_size

    dest_base.parent.mkdir(parents=True, exist_ok=True)
    part = dest_base.with_name(dest_base.name + ".part")
    with session.get(url, stream=True, timeout=120, headers={"User-Agent": USER_AGENT}) as resp:
        resp.raise_for_status()
        ext = _ext_for(resp.headers.get("content-type", ""), url)
        size = 0
        with part.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                size += len(chunk)
    final = dest_base.with_name(dest_base.name + ext)
    part.replace(final)
    return final, size

