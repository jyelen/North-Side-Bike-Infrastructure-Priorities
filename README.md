# North Side Bike Network

An interactive map and ranking tool for a connected protected bike network across Chicago Wards 33, 40, 46, 47, 48 and 49. The live page is `index.html`, served by GitHub Pages.

## How the data is refreshed

`index.html` is built. Don't edit it by hand: change `src/template.html` or the pipeline, then rebuild.

```
pip install -r requirements.txt
python pipeline/run.py              # download everything, build, check, write index.html
python pipeline/run.py --no-fetch   # rebuild from files already in data/raw
```

The steps:

| Step | File | What it does |
|---|---|---|
| Download | `pipeline/fetch.py` | Pulls each source into `data/raw/` (not committed) |
| Build | `pipeline/build_data.py` | Measures every segment and writes `data/build/bikenet_data.json` |
| Check | `pipeline/checks.py` | Compares with the committed version, writes `data/build/refresh_report.md`, and stops if something looks broken |
| Page | `pipeline/build_page.py` | Puts the data into `src/template.html` and writes `index.html` |

The **Refresh data** GitHub Action runs the same steps on GitHub's servers. Right now you start it by hand (Actions tab, then Run workflow). Pushing to the `pipeline-test` branch also runs it and saves the results to that branch.

## Sources

| Data | Source | Refreshed |
|---|---|---|
| Bike routes | Chicago Data Portal `hvv9-38ut` (CDOT) | Each run. CDOT updates it a few times a year |
| Crashes and injured people | `85ca-t3if`, `u6pd-qa9d` | Each run (daily at the source) |
| Business licenses | `uupf-x98q` (current active) | Each run |
| Wards, zoning | `p293-wvbd`, `dj47-wfun` | Each run |
| Street centerlines | `pr57-gg9e` (the data behind the Street Center Lines map). Falls back to `data/static/streets.geojson` | Each run |
| L stations, ridership | `8pix-ypme`, `5neh-572f` (last full calendar year) | Each run |
| Census (population, commuting, car ownership, poverty) | ACS 5-year table summary files, block groups, Cook County. Uses the newest release found | Yearly (December) |
| Road width, lanes, parking, traffic, speed, owner | IDOT road inventory, newest `cis_LocationTool<year>` service | Yearly |

## Not refreshed automatically

- **`data/manual/manual_segments.json`**: facilities reported in the news but not yet in CDOT's file. The check report flags entries that CDOT's file now covers, so they can be removed.
- **`data/static/bike_route_snapshots.json`**: archived city bike maps (2014–2022) used for segment history.
- Fatality stories, cost assumptions, weights and research sources live in `src/template.html` and are edited by hand.
