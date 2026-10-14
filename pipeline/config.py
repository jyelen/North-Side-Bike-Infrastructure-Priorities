"""Settings shared by every pipeline step.

Everything that a person might reasonably want to change (which wards, which
datasets, the start date for crash data) lives here, not in the step scripts.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"          # downloaded each run (not committed)
STATIC = ROOT / "data" / "static"    # historical data that never changes (committed)
MANUAL = ROOT / "data" / "manual"    # hand-curated additions (committed, edit by hand)
BUILD = ROOT / "data" / "build"      # intermediate + final outputs
SRC = ROOT / "src"                   # page template and styles

WARDS = [33, 40, 46, 47, 48, 49]
CRASH_START = "2021-01-01"

# Chicago Data Portal (Socrata) dataset IDs
CHI = "https://data.cityofchicago.org"
DATASETS = {
    "bike_routes": "hvv9-38ut",   # Bike Routes (CDOT)
    "wards":       "p293-wvbd",   # Boundaries - Wards (2023-)
    "streets":     "pr57-gg9e",   # transportation (data behind the "Street Center Lines" map, 6imu-meau)
    "zoning":      "dj47-wfun",   # Boundaries - Zoning Districts (current)
    "crashes":     "85ca-t3if",   # Traffic Crashes - Crashes
    "people":      "u6pd-qa9d",   # Traffic Crashes - People
    "businesses":  "uupf-x98q",   # Business Licenses - Current Active
    "l_stops":     "8pix-ypme",   # CTA - System Information - List of 'L' Stops
    "l_ridership": "5neh-572f",   # CTA - Ridership - 'L' Station Entries - Daily Totals
}

# Census: ACS 5-year estimates by block group, Cook County
STATE, COUNTY = "17", "031"
ACS_TABLES = {
    # internal name used by the build step : Census API variable
    "B01003_E001": "B01003_001E",  # total population
    "B08301_E001": "B08301_001E",  # workers 16+
    "B08301_E010": "B08301_010E",  # public transportation
    "B08301_E018": "B08301_018E",  # bicycle
    "B08301_E019": "B08301_019E",  # walked
    "B08301_E021": "B08301_021E",  # worked from home
    "B25044_E001": "B25044_001E",  # occupied housing units
    "B25044_E003": "B25044_003E",  # owner occupied, no vehicle
    "B25044_E010": "B25044_010E",  # renter occupied, no vehicle
    "C17002_E001": "C17002_001E",  # poverty status determined
    "C17002_E002": "C17002_002E",  # income/poverty ratio under .50
    "C17002_E003": "C17002_003E",  # .50 to .99
}

# IDOT road inventory (ArcGIS). The service name carries the year
# (cis_LocationTool2026); fetch.py looks for the newest one.
IDOT_SERVER = "https://gis1.dot.illinois.gov/arcgis/rest/services"
IDOT_LAYER_NAME = "ROAD NETWORK"

# Date of the news report behind data/manual/manual_segments.json
MANUAL_SOURCE_DATE = "2026-09-30"
