from pathlib import Path

import pytest

from meta_ads.snapshot import extract_media, find_snapshot, media_from_snapshot

PAGE = (Path(__file__).parent / "fixtures" / "ad_page.html").read_text()


def names(items):
    return [(i.kind, i.quality, i.url.split("/")[-1].split("?")[0]) for i in items]


def test_picks_only_the_requested_ad():
    assert names(media_from_snapshot(find_snapshot(PAGE, "111"))) == [("image", "original", "OTHER_AD.jpg")]


def test_video_and_carousel_cards():
    items = media_from_snapshot(find_snapshot(PAGE, "222"))
    assert names(items) == [
        ("video", "hd", "vid_hd.mp4"),
        ("video_thumb", "preview", "thumb.jpg"),
        ("image", "original", "card_a.jpg"),
        ("video", "sd", "card_b.mp4"),
        ("image", "resized", "card_b_s.jpg"),
    ]
    assert items[0].url == "https://video.xx.fbcdn.net/v/vid_hd.mp4?oh=a&oe=b"


def test_missing_ad():
    assert find_snapshot(PAGE, "999") is None


class FakeResp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """First GET returns Facebook's JS challenge; after the verify POST, the real page."""

    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if len(self.calls) == 1:
            return FakeResp("<script>fetch('/__rd_verify_abc?challenge=3', {method: 'POST'})</script>", 403)
        return FakeResp(PAGE)

    def post(self, url, **kw):
        self.calls.append(("POST", url))
        return FakeResp("")


def test_passes_challenge_then_extracts():
    s = FakeSession()
    items = extract_media(s, "222")
    assert len(items) == 5
    assert s.calls == [
        ("GET", "https://www.facebook.com/ads/library/?id=222"),
        ("POST", "https://www.facebook.com/__rd_verify_abc?challenge=3"),
        ("GET", "https://www.facebook.com/ads/library/?id=222"),
    ]


def test_ad_not_on_page_raises():
    s = FakeSession()
    s.calls.append(("GET", "skip-challenge"))
    with pytest.raises(RuntimeError, match="not found"):
        extract_media(s, "999")
