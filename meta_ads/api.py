"""Thin client for GET /ads_archive with pagination, retries and rate-limit handling."""

import json
import logging
import time
from collections.abc import Iterator

import requests

from meta_ads.config import FIELDS
from meta_ads.storage import redact

log = logging.getLogger(__name__)

# Graph API error codes worth retrying (throttling / transient).
RETRYABLE_CODES = {1, 2, 4, 17, 32, 341, 613, 80004}


class AdLibraryError(RuntimeError):
    pass


def build_params(
    *,
    ad_reached_countries: list[str],
    search_terms: str | None = None,
    search_page_ids: list[str] | None = None,
    ad_active_status: str = "ALL",
    ad_delivery_date_min: str | None = None,
    ad_delivery_date_max: str | None = None,
    languages: list[str] | None = None,
    media_type: str = "ALL",
    limit: int = 250,
) -> dict[str, str]:
    if not ad_reached_countries:
        raise ValueError("ad_reached_countries is required")
    if not search_terms and not search_page_ids:
        raise ValueError("search_terms or search_page_ids is required")
    if search_page_ids and len(search_page_ids) > 10:
        raise ValueError("search_page_ids accepts at most 10 page IDs")

    params = {
        "fields": ",".join(FIELDS),
        "ad_type": "ALL",
        "ad_reached_countries": json.dumps([c.upper() for c in ad_reached_countries]),
        "ad_active_status": ad_active_status.upper(),
        "media_type": media_type.upper(),
        "limit": str(limit),
    }
    if search_terms:
        params["search_terms"] = search_terms
    if search_page_ids:
        params["search_page_ids"] = json.dumps([str(p) for p in search_page_ids])
    if ad_delivery_date_min:
        params["ad_delivery_date_min"] = ad_delivery_date_min
    if ad_delivery_date_max:
        params["ad_delivery_date_max"] = ad_delivery_date_max
    if languages:
        params["languages"] = json.dumps([l.lower() for l in languages])
    return params


class AdLibraryClient:
    def __init__(
        self,
        access_token: str,
        api_version: str,
        session: requests.Session | None = None,
        max_retries: int = 6,
    ):
        self.access_token = access_token
        self.base_url = f"https://graph.facebook.com/{api_version}/ads_archive"
        self.session = session or requests.Session()
        self.max_retries = max_retries

    def iter_ads(self, params: dict[str, str], max_ads: int | None = None) -> Iterator[dict]:
        url: str | None = self.base_url
        query: dict[str, str] | None = {**params, "access_token": self.access_token}
        yielded = 0
        while url:
            payload = self._get(url, query)
            for ad in payload.get("data", []):
                yield ad
                yielded += 1
                if max_ads and yielded >= max_ads:
                    return
            # paging.next already carries every query param, including the token.
            url = payload.get("paging", {}).get("next")
            query = None

    def _get(self, url: str, params: dict[str, str] | None) -> dict:
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise AdLibraryError(redact(f"Network error: {e}")) from None
                self._backoff(attempt, "network error")
                continue

            self._respect_usage_headers(resp)
            try:
                payload = resp.json()
            except ValueError:
                payload = {}

            error = payload.get("error")
            if resp.ok and not error:
                return payload

            code = (error or {}).get("code")
            retryable = resp.status_code == 429 or resp.status_code >= 500 or code in RETRYABLE_CODES
            message = redact((error or {}).get("message") or resp.text[:300])
            if not retryable or attempt == self.max_retries:
                raise AdLibraryError(f"HTTP {resp.status_code}, code {code}: {message}")
            self._backoff(attempt, f"code {code}: {message}")
        raise AssertionError("unreachable")

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = min(300, 5 * 2**attempt)
        log.warning("API retry in %ss (%s)", delay, reason)
        time.sleep(delay)

    @staticmethod
    def _respect_usage_headers(resp: requests.Response) -> None:
        """Sleep proactively when Meta reports we're close to the rate limit."""
        usage_pct = 0
        for header in ("x-app-usage", "x-business-use-case-usage", "x-ad-account-usage"):
            raw = resp.headers.get(header)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            entries = [e for v in data.values() for e in v] if header == "x-business-use-case-usage" else [data]
            for entry in entries:
                for key in ("call_count", "total_cputime", "total_time", "acc_id_util_pct"):
                    val = entry.get(key)
                    if isinstance(val, (int, float)):
                        usage_pct = max(usage_pct, val)
                wait_min = entry.get("estimated_time_to_regain_access")
                if isinstance(wait_min, (int, float)) and wait_min > 0:
                    log.warning("Rate limited; sleeping %s min as advised by Meta", wait_min)
                    time.sleep(wait_min * 60)
                    return
        if usage_pct >= 90:
            log.warning("API usage at %s%%, pausing 60s", usage_pct)
            time.sleep(60)
