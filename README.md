# Meta Ad Library scraper

Queries the [Meta Ad Library API](https://www.facebook.com/ads/library/api/) and **downloads every
image and video** from each ad's `ad_snapshot_url`. Results go into a single CSV, with one row per ad
and a column listing that ad's downloaded media files.

## Desktop app (Windows / macOS)

No Python needed. The app is a small window: paste your token, fill in the filters, click **Start**.
Results go to `Documents/MetaAdsScraper/<timestamp>/` by default. The form, including the token, is
remembered between runs (in `%APPDATA%\MetaAdsScraper\settings.json` on Windows, `~/Library/Application Support/MetaAdsScraper/settings.json` on macOS).

**Download** (public, no GitHub account needed; the repo must be public):
- Windows: `https://github.com/<owner>/<repo>/releases/latest/download/MetaAdsScraper.exe`
- macOS: `https://github.com/<owner>/<repo>/releases/latest/download/MetaAdsScraper-macos.zip`

**Building:** `.github/workflows/build.yml` builds both apps on GitHub's Windows and macOS machines.
Every push to `main` replaces the **latest** release. Pushing a tag like `v1.0` also creates a permanent `v1.0` release.

- **Windows:** `MetaAdsScraper.exe`. Windows SmartScreen will warn because the app isn't signed:
  click **More info → Run anyway**.
- **macOS:** `MetaAdsScraper-macos.zip` → unzip → `MetaAdsScraper.app` (Apple Silicon Macs). The app
  isn't signed, so the first time you open it, right-click it → **Open** → **Open**. If macOS says it is
  "damaged", run `xattr -dr com.apple.quarantine MetaAdsScraper.app` once.

To run the window from source: `python app.py`.

## Command line

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # paste your META_ACCESS_TOKEN
```

Token: confirm your identity at https://www.facebook.com/ID, create an app at developers.facebook.com,
then generate a User access token in the Graph API Explorer.

### Usage

```bash
python -m meta_ads --search-terms "running shoes" --countries PL,DE \
  --active-status ALL --date-min 2026-01-01 --date-max 2026-09-30 \
  --languages pl --media-type ALL --max-ads 500

python -m meta_ads --search-page-ids 123456789,987654321 --countries PL

python -m meta_ads ... --resume output/20261004_120000   # continue / retry failed media
python -m meta_ads ... --skip-media                       # CSV only, no downloads
```

## Output

```
output/<timestamp>/
  ads.csv
  media/<page_id>/<ad_id>/video_01_hd.mp4, video_01_thumb.jpg, image_01.jpg, ...
```

`ads.csv` columns: `id, page_id, page_name, ad_delivery_start_time, ad_delivery_stop_time,
ad_snapshot_url, age_country_gender_reach_breakdown, media_count, media_files, media_error`.
`media_files` holds `;`-separated paths relative to the run folder. The reach breakdown is a JSON string.
The access token is stripped from `ad_snapshot_url`.

## How media is fetched

For each ad, the tool opens `https://www.facebook.com/ads/library/?id=<id>` (no token needed) and reads the
ad's embedded JSON. It downloads every creative: all carousel cards, all video versions (HD when available,
otherwise SD, plus a thumbnail) and all images (original size). Files are numbered `image_01.jpg`,
`video_01_hd.mp4`, `video_01_thumb.jpg` and so on.

## Limitations (Meta-side)

- With `ad_type=ALL`, the API only returns ads **delivered in the EU/UK**. Outside the EU, only
  political/issue ads are available.
- `age_country_gender_reach_breakdown` is only filled in for EU-delivered ads.
