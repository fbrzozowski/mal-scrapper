import json

import pytest

from meta_ads.api import AdLibraryClient, AdLibraryError, Filters, build_params
from meta_ads.storage import redact, strip_token


def test_build_params_serialises_lists():
    p = build_params(
        Filters(
            countries=["pl", "de"],
            search_terms="shoes",
            search_page_ids=["1", "2"],
            active_status="active",
            date_min="2026-01-01",
            date_max="2026-02-01",
            languages=["PL"],
        )
    )
    assert json.loads(p["ad_reached_countries"]) == ["PL", "DE"]
    assert json.loads(p["search_page_ids"]) == ["1", "2"]
    assert json.loads(p["languages"]) == ["pl"]
    assert p["ad_active_status"] == "ACTIVE"
    assert p["media_type"] == "ALL"
    assert p["ad_type"] == "ALL"
    assert "age_country_gender_reach_breakdown" in p["fields"].split(",")


def test_build_params_requires_search():
    with pytest.raises(ValueError):
        build_params(Filters(countries=["PL"]))
    with pytest.raises(ValueError):
        build_params(Filters(countries=[], search_terms="x"))


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = status < 400
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def test_pagination_follows_next_and_respects_max():
    session = FakeSession(
        [
            FakeResp({"data": [{"id": "1"}, {"id": "2"}], "paging": {"next": "https://graph/next?after=x"}}),
            FakeResp({"data": [{"id": "3"}, {"id": "4"}]}),
        ]
    )
    client = AdLibraryClient("TOKEN", "v24.0", session=session)
    ids = [a["id"] for a in client.iter_ads({"search_terms": "x"})]
    assert ids == ["1", "2", "3", "4"]
    assert session.calls[0][1]["access_token"] == "TOKEN"
    assert session.calls[1] == ("https://graph/next?after=x", None)

    session = FakeSession([FakeResp({"data": [{"id": "1"}, {"id": "2"}], "paging": {"next": "n"}})])
    client = AdLibraryClient("TOKEN", "v24.0", session=session)
    assert [a["id"] for a in client.iter_ads({}, max_ads=1)] == ["1"]


def test_non_retryable_error_is_redacted():
    session = FakeSession([FakeResp({"error": {"code": 190, "message": "bad access_token=SECRET123"}}, status=400)])
    client = AdLibraryClient("SECRET123", "v24.0", session=session)
    with pytest.raises(AdLibraryError) as e:
        list(client.iter_ads({}))
    assert "SECRET123" not in str(e.value)


def test_token_helpers():
    url = "https://www.facebook.com/ads/archive/render_ad/?id=42&access_token=SECRET"
    assert strip_token(url) == "https://www.facebook.com/ads/archive/render_ad/?id=42"
    assert "SECRET" not in redact(f"failed GET {url}")


def test_web_search_urls_one_per_country_and_page():
    from urllib.parse import parse_qs, urlsplit

    from meta_ads.web import search_urls

    urls = search_urls(Filters(countries=["pl", "de"], search_page_ids=["11", "22"], languages=["pl"], date_min="2026-01-01"))
    assert len(urls) == 4
    q = parse_qs(urlsplit(urls[0]).query)
    assert q["country"] == ["PL"] and q["view_all_page_id"] == ["11"] and q["search_type"] == ["page"]
    assert q["content_languages[0]"] == ["pl"] and q["start_date[min]"] == ["2026-01-01"]

    q = parse_qs(urlsplit(search_urls(Filters(countries=["PL"], search_terms="bank"))[0]).query)
    assert q["q"] == ["bank"] and q["search_type"] == ["keyword_unordered"] and q["active_status"] == ["all"]


def test_web_result_mapped_to_api_fields():
    from meta_ads.web import to_api_shape

    ad = to_api_shape({"ad_archive_id": "5", "page_id": "9", "page_name": "P", "start_date": 1790751600, "end_date": 1791097200, "is_active": False, "snapshot": {}})
    assert ad["id"] == "5" and ad["ad_delivery_start_time"] == "2026-09-30" and ad["ad_delivery_stop_time"] == "2026-10-04"
    assert ad["ad_snapshot_url"] == "https://www.facebook.com/ads/library/?id=5"
    assert to_api_shape({"ad_archive_id": "5", "start_date": 1790751600, "end_date": 1791097200, "is_active": True})["ad_delivery_stop_time"] == ""
