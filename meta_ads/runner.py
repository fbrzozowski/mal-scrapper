"""One scrape run: query the API, download each ad's media, write ads.csv. Shared by the CLI and the GUI."""

import logging
import time
from collections.abc import Callable
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from meta_ads.api import AdLibraryClient, AdLibraryError, Filters, build_params
from meta_ads.downloader import download
from meta_ads.snapshot import extract_media, media_from_snapshot
from meta_ads.storage import append_row, dedupe, read_rows, redact, to_row

log = logging.getLogger("meta_ads")


@dataclass
class RunResult:
    csv_path: Path
    processed: int = 0
    media_count: int = 0
    failed: int = 0
    api_error: str | None = None
    stopped: bool = False


def _make_session(pool: int) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


class MediaFetcher:
    def __init__(self, workers: int, delay: float):
        self.delay = delay
        self.page_session = _make_session(2)
        self.dl_session = _make_session(workers)
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self._last_fetch = 0.0

    def close(self):
        self.pool.shutdown()

    def _throttle(self):
        wait = self.delay - (time.monotonic() - self._last_fetch)
        if wait > 0:
            time.sleep(wait)
        self._last_fetch = time.monotonic()

    def fetch(self, ad: dict, run_dir: Path) -> tuple[list[str], str]:
        """Extract and download all media for one ad. Returns (relative file paths, error text)."""
        ad_dir = run_dir / "media" / str(ad.get("page_id", "unknown")) / str(ad["id"])
        try:
            if "snapshot" in ad:  # website mode: media URLs came with the search results
                items = media_from_snapshot(ad["snapshot"])
            else:
                self._throttle()
                items = extract_media(self.page_session, str(ad["id"]))
        except Exception as e:
            return [], redact(str(e))

        counters = {"image": 0, "video": 0, "video_thumb": 0}
        jobs = []
        for item in items:
            counters[item.kind] += 1
            n = counters[item.kind]
            name = {
                "image": f"image_{n:02d}",
                "video": f"video_{n:02d}_{item.quality}",
                "video_thumb": f"video_{n:02d}_thumb",
            }[item.kind]
            jobs.append((item, ad_dir / name))

        def run(job):
            item, dest = job
            try:
                path, _ = download(self.dl_session, item.url, dest)
                return path.relative_to(run_dir).as_posix(), None
            except Exception as e:
                return None, f"{dest.name}: {redact(str(e))}"

        files, errors = [], []
        for path, err in self.pool.map(run, jobs):
            if path:
                files.append(path)
            if err:
                errors.append(err)

        if not files and not errors:
            errors.append("no media found for this ad")
        return files, "; ".join(errors)


def new_run_dir(out_dir: Path) -> Path:
    return out_dir / datetime.now().strftime("%Y%m%d_%H%M%S")


def run_scrape(
    *,
    token: str | None,
    api_version: str,
    filters: Filters,
    run_dir: Path,
    max_ads: int | None = None,
    skip_media: bool = False,
    workers: int = 4,
    delay: float = 1.0,
    on_ad: Callable[[RunResult], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> RunResult:
    """Run (or resume, if run_dir already has ads.csv) a scrape. Calls on_ad after each ad.

    With a token the official API is used; without one, the public website in a headless browser.
    """
    filters.validate()
    run_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(csv_path=run_dir / "ads.csv")

    # Ads that already have all their media are skipped; failed ones are retried.
    done_ids = {r["id"] for r in read_rows(result.csv_path) if skip_media or (r["media_files"] and not r["media_error"])}
    if done_ids:
        log.info("Resuming: %d ads already complete", len(done_ids))

    fetcher = None if skip_media else MediaFetcher(workers, delay)
    with ExitStack() as stack:
        if token:
            log.info("Using the Meta API (access token provided)")
            web = None
            ads = AdLibraryClient(token, api_version).iter_ads(build_params(filters), max_ads)
        else:
            from meta_ads.web import WebSearch

            log.info("No access token: searching the Ad Library website in a headless browser")
            web = stack.enter_context(WebSearch(delay))
            ads = web.iter_ads(filters, max_ads)
        try:
            for ad in ads:
                if should_stop and should_stop():
                    result.stopped = True
                    break
                if ad["id"] in done_ids:
                    continue
                if web:
                    ad["age_country_gender_reach_breakdown"], _ = web.reach_breakdown(ad, filters.countries[0])
                files, error = [], ""
                if fetcher:
                    files, error = fetcher.fetch(ad, run_dir)
                    if error:
                        result.failed += 1
                        log.warning("ad %s: %s", ad["id"], error)
                append_row(result.csv_path, to_row(ad, files, error))
                result.processed += 1
                result.media_count += len(files)
                if on_ad:
                    on_ad(result)
        except AdLibraryError as e:
            result.api_error = str(e)
            log.error("API error: %s", result.api_error)
        except RuntimeError as e:
            result.api_error = str(e)
            log.error("%s", e)
        finally:
            if fetcher:
                fetcher.close()
            if result.csv_path.exists():
                dedupe(result.csv_path)
    return result
