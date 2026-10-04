import os

from dotenv import load_dotenv

DEFAULT_API_VERSION = "v24.0"

FIELDS = [
    "id",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "ad_snapshot_url",
    "age_country_gender_reach_breakdown",
    "page_id",
    "page_name",
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


def load_settings() -> tuple[str, str]:
    """Return (access_token, api_version) from env / .env."""
    load_dotenv()
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "META_ACCESS_TOKEN is not set. Put it in .env or export it "
            "(see .env.example)."
        )
    version = os.environ.get("GRAPH_API_VERSION", DEFAULT_API_VERSION).strip()
    return token, version
