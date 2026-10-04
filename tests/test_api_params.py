import json

import pytest

from meta_ads.api import AdLibraryClient, AdLibraryError, build_params
from meta_ads.storage import redact, strip_token


def test_build_params_serialises_lists():
    p = build_params(
        ad_reached_countries=["pl", "de"],
        search_terms="shoes",
        search_page_ids=["1", "2"],
        ad_active_status="active",
        ad_delivery_date_min="2026-01-01",
        ad_delivery_date_max="2026-02-01",
        languages=["PL"],
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
        build_params(ad_reached_countries=["PL"])
    with pytest.raises(ValueError):
        build_params(ad_reached_countries=[], search_terms="x")


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
