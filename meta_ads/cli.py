"""CLI: query the Ad Library API, save ad metadata and download every creative."""

import argparse
import logging
import sys
from pathlib import Path

from tqdm import tqdm

from meta_ads.api import build_params
from meta_ads.config import load_settings
from meta_ads.runner import new_run_dir, run_scrape

log = logging.getLogger("meta_ads")


def _csv_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="meta-ads",
        description="Fetch ads from the Meta Ad Library API and download their images/videos.",
    )
    q = p.add_argument_group("query")
    q.add_argument("--search-terms", help="Keyword search (max 100 chars)")
    q.add_argument("--search-page-ids", type=_csv_list, help="Comma-separated page IDs (max 10)")
    q.add_argument("--countries", type=_csv_list, required=True, help="ad_reached_countries, e.g. PL,DE")
    q.add_argument("--active-status", default="ALL", choices=["ALL", "ACTIVE", "INACTIVE"])
    q.add_argument("--date-min", help="ad_delivery_date_min, YYYY-mm-dd")
    q.add_argument("--date-max", help="ad_delivery_date_max, YYYY-mm-dd")
    q.add_argument("--languages", type=_csv_list, help="ISO 639-1 codes, e.g. pl,en")
    q.add_argument("--media-type", default="ALL", choices=["ALL", "IMAGE", "MEME", "VIDEO", "NONE"])
    q.add_argument("--max-ads", type=int, help="Stop after this many ads")
    q.add_argument("--page-size", type=int, default=250, help="API page size (limit)")

    o = p.add_argument_group("output / media")
    o.add_argument("--out", type=Path, default=Path("output"), help="Base output directory")
    o.add_argument("--resume", type=Path, help="Existing run directory to continue")
    o.add_argument("--skip-media", action="store_true", help="Only save metadata")
    o.add_argument("--workers", type=int, default=4, help="Parallel downloads per ad")
    o.add_argument("--delay", type=float, default=1.0, help="Seconds between Ad Library page fetches")
    o.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args(argv)
    if not args.search_terms and not args.search_page_ids:
        p.error("one of --search-terms or --search-page-ids is required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    token, api_version = load_settings()

    try:
        params = build_params(
            ad_reached_countries=args.countries,
            search_terms=args.search_terms,
            search_page_ids=args.search_page_ids,
            ad_active_status=args.active_status,
            ad_delivery_date_min=args.date_min,
            ad_delivery_date_max=args.date_max,
            languages=args.languages,
            media_type=args.media_type,
            limit=args.page_size,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    run_dir = args.resume or new_run_dir(args.out)
    bar = tqdm(desc="ads", unit="ad")
    try:
        result = run_scrape(
            token=token,
            api_version=api_version,
            params=params,
            run_dir=run_dir,
            max_ads=args.max_ads,
            skip_media=args.skip_media,
            workers=args.workers,
            delay=args.delay,
            on_ad=lambda r: bar.update(1),
        )
    except KeyboardInterrupt:
        log.warning("Interrupted; partial results saved. Re-run with --resume %s", run_dir)
        return 130
    finally:
        bar.close()

    print(f"\nDone: {result.processed} ads, {result.media_count} media files, {result.failed} ads with media errors")
    print(f"Output: {result.csv_path}")
    return 1 if result.api_error else 0
