"""No-token mode: search the public Ad Library website in a headless browser.

The first ~30 results are embedded in the search page's HTML; scrolling makes the page
fetch 10 more at a time (AdLibrarySearchPaginationQuery), which we capture. The reach
breakdown comes from the same request the "See ad details" dialog makes.
"""

import json
import logging
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from urllib.parse import urlencode

from meta_ads.api import Filters

log = logging.getLogger("meta_ads")

SEARCH_URL = "https://www.facebook.com/ads/library/?"
# Facebook changes this id when it redeploys; discover_details_doc_id() finds the current one.
DETAILS_DOC_ID = "25068828942793558"
DETAILS_OPERATION = "AdLibraryV3AdDetailsQuery"

_DETAILS_JS = """async ([lsd, docId, vars]) => {
  const body = new URLSearchParams({av: '0', __user: '0', __a: '1', lsd, fb_api_caller_class: 'RelayModern',
    fb_api_req_friendly_name: 'AdLibraryV3AdDetailsQuery', variables: JSON.stringify(vars),
    server_timestamps: 'true', doc_id: docId});
  const r = await fetch('/api/graphql/', {method: 'POST', body,
    headers: {'X-FB-LSD': lsd, 'X-FB-Friendly-Name': 'AdLibraryV3AdDetailsQuery'}});
  return await r.text();
}"""

_FIND_DOC_ID_JS = """async (name) => {
  const urls = [...new Set(performance.getEntriesByType('resource').map(e => e.name)
    .concat([...document.scripts].map(s => s.src)).filter(u => /rsrc\\.php.*\\.js/.test(u)))];
  const re = new RegExp('__d\\\\("' + name + '_facebookRelayOperation",\\\\[\\\\],\\\\(function\\\\([^)]*\\\\)\\\\{\\\\w+\\\\.exports="(\\\\d+)"');
  for (const u of urls) {
    try { const m = (await (await fetch(u)).text()).match(re); if (m) return m[1]; } catch (e) {}
  }
  return null;
}"""


def search_urls(f: Filters) -> list[str]:
    """One URL per (country, page id) combination; the website takes a single country / page per search."""
    base = [
        ("active_status", f.active_status.lower()),
        ("ad_type", "all"),
        ("media_type", f.media_type.lower()),
    ]
    for i, lang in enumerate(f.languages or []):
        base.append((f"content_languages[{i}]", lang.lower()))
    if f.date_min:
        base.append(("start_date[min]", f.date_min))
    if f.date_max:
        base.append(("start_date[max]", f.date_max))

    urls = []
    for country in f.countries:
        if f.search_page_ids:
            for page_id in f.search_page_ids:
                q = [("country", country.upper()), ("view_all_page_id", page_id), ("search_type", "page")]
                if f.search_terms:
                    q.append(("q", f.search_terms))
                urls.append(SEARCH_URL + urlencode(base + q))
        else:
            q = [("country", country.upper()), ("q", f.search_terms), ("search_type", "keyword_unordered")]
            urls.append(SEARCH_URL + urlencode(base + q))
    return urls


def _date(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else ""


def to_api_shape(ad: dict) -> dict:
    """Map a website search result onto the same fields the Graph API returns."""
    return {
        "id": ad["ad_archive_id"],
        "page_id": ad.get("page_id", ""),
        "page_name": ad.get("page_name", ""),
        "ad_delivery_start_time": _date(ad.get("start_date")),
        "ad_delivery_stop_time": "" if ad.get("is_active") else _date(ad.get("end_date")),
        "ad_snapshot_url": f"https://www.facebook.com/ads/library/?id={ad['ad_archive_id']}",
        "snapshot": ad.get("snapshot") or {},
    }


def _ads_in(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    ads = []
    for m in re.finditer(r'\{"ad_archive_id":"\d+"', text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if "snapshot" in obj:
            ads.append(obj)
    return ads


def ensure_browser_installed() -> None:
    """Download Playwright's Chromium on first use (one-time, ~150 MB)."""
    from playwright._impl._driver import compute_driver_executable, get_driver_env

    log.info("Downloading the headless browser (one-time, ~150 MB)…")
    driver = compute_driver_executable()
    cmd = [*driver, "install", "chromium"] if isinstance(driver, tuple) else [str(driver), "install", "chromium"]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    subprocess.run(cmd, env=get_driver_env(), check=True, creationflags=flags)


class WebSearch:
    """Headless-browser search. Use as a context manager from a single thread."""

    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self._pw = self._browser = self._page = None
        self._lsd: str | None = None
        self._details_doc_id = DETAILS_DOC_ID
        self._batches: list[list[dict]] = []
        self._has_next = True
        self._doc_html: str | None = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._launch()
        context = self._browser.new_context(locale="en-US", viewport={"width": 1280, "height": 900})
        self._page = context.new_page()
        self._page.on("response", self._on_response)
        return self

    def __exit__(self, *exc):
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def _launch(self):
        chromium = self._pw.chromium
        # Use a browser that's already installed (Edge ships with Windows), else Playwright's own Chromium.
        for channel in (["msedge", "chrome"] if sys.platform == "win32" else ["chrome"]):
            try:
                return chromium.launch(channel=channel, headless=True)
            except Exception:
                pass
        try:
            return chromium.launch(headless=True)
        except Exception as e:
            if "Executable doesn't exist" not in str(e):
                raise
        ensure_browser_installed()
        return chromium.launch(headless=True)

    def _on_response(self, resp):
        try:
            if resp.request.resource_type == "document" and "/ads/library" in resp.url:
                self._doc_html = resp.text()
            elif "/api/graphql" in resp.url and resp.request.headers.get("x-fb-friendly-name") == "AdLibrarySearchPaginationQuery":
                for line in resp.text().split("\n"):
                    if '"search_results_connection"' not in line:
                        continue
                    conn = json.loads(line)["data"]["ad_library_main"]["search_results_connection"]
                    self._batches.append([a for e in conn["edges"] for a in e["node"]["collated_results"]])
                    self._has_next = conn["page_info"]["has_next_page"]
                    if not self._has_next:
                        log.info("Reached the end of the results")
                if "Rate limit exceeded" in resp.text():
                    log.warning("Facebook rate-limited scrolling; waiting before trying again")
        except Exception as e:  # never break the browser's event loop
            log.debug("response handler: %s", e)

    def _load(self, url: str) -> list[dict]:
        self._batches.clear()
        self._has_next = True
        self._doc_html = None
        self._page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        self._page.wait_for_timeout(2500)
        self._lsd = self._page.evaluate("() => { try { return require('LSD').token } catch (e) { return null } }")
        html = self._doc_html or ""
        if '"xfb_ad_library_is_captcha_required":true' in html:
            raise RuntimeError("Facebook is asking for a captcha. Wait a while and try again.")
        count = re.search(r'"search_results_connection":\{"count":(\d+)', html)
        if count:
            log.info("Facebook reports about %s matching ads", count.group(1))
        if '"has_next_page":false' in html:
            self._has_next = False
        return _ads_in(html)

    def iter_ads(self, filters: Filters, max_ads: int | None = None) -> Iterator[dict]:
        seen: set[str] = set()
        for url in search_urls(filters):
            log.info("Searching: %s", url)
            pending = self._load(url)
            idle = 0
            while True:
                for ad in pending:
                    if ad["ad_archive_id"] in seen:
                        continue
                    seen.add(ad["ad_archive_id"])
                    yield to_api_shape(ad)
                    if max_ads and len(seen) >= max_ads:
                        return
                pending = []
                if not self._has_next:
                    break
                self._page.mouse.wheel(0, 20_000)
                self._page.wait_for_timeout(int(max(self.delay, 1.0) * 1500))
                if self._batches:
                    pending = [a for batch in self._batches for a in batch]
                    self._batches.clear()
                    idle = 0
                else:
                    idle += 1
                    if idle >= 3:
                        # Nothing new for a while: back off, then give up on this search.
                        self._page.wait_for_timeout(15_000 * idle)
                    if idle >= 6:
                        log.warning("No more results loaded; stopping this search (%d ads so far)", len(seen))
                        break

    def reach_breakdown(self, ad: dict, country: str) -> tuple[list | None, int | None]:
        """Return (age_country_gender_reach_breakdown, eu_total_reach) for one ad, like the details dialog."""
        if not self._lsd:
            return None, None
        variables = {
            "adArchiveID": ad["id"], "pageID": ad["page_id"], "country": country.upper(),
            "sessionID": str(uuid.uuid4()), "source": None, "isAdNonPolitical": True, "isAdNotAAAEligible": False,
        }
        for attempt in range(2):
            raw = self._page.evaluate(_DETAILS_JS, [self._lsd, self._details_doc_id, variables])
            data = json.loads(raw.split("\n")[0] or "{}")
            if "errors" not in data:
                break
            if attempt == 0 and not any("Rate limit" in e.get("message", "") for e in data["errors"]):
                # Most likely Facebook changed the query id; look up the current one once.
                found = self._page.evaluate(_FIND_DOC_ID_JS, DETAILS_OPERATION)
                if found and found != self._details_doc_id:
                    log.info("Updated ad-details query id")
                    self._details_doc_id = found
                    continue
            log.debug("details error for %s: %s", ad["id"], data["errors"])
            return None, None
        self._page.wait_for_timeout(int(self.delay * 1000))
        return _find_key(data, "age_country_gender_reach_breakdown"), _find_key(data, "eu_total_reach")


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find_key(child, key)
        if found is not None:
            return found
    return None
