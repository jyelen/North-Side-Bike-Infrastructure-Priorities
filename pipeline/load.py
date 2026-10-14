"""Read the raw downloads into geometry and tables.

All geometry is converted to Illinois State Plane East (EPSG:3435, feet) so
distances and buffers are in feet.
"""
import collections
import json
import re

import pandas as pd
import shapefile
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.geometry import mapping
from shapely.ops import linemerge, substring, transform, unary_union
from shapely.strtree import STRtree

from config import RAW, MANUAL, WARDS

SQMI = 27878400
tr = Transformer.from_crs(4326, 3435, always_xy=True).transform
inv = Transformer.from_crs(3435, 4326, always_xy=True).transform


def pt(lon, lat):
    return Point(*tr(lon, lat))


def derived_cf(p):
    """Contraflow: a two-way bike route on a one-way street."""
    return p.get('oneway_dir') not in ('-', None) and p.get('br_oneway') == '2W'


# --------------------------------------------------------------------------
# Wards and bike routes
# --------------------------------------------------------------------------
def load_wards():
    wards = {}
    for f in json.load(open(RAW / 'wards.geojson'))['features']:
        p = f['properties']
        if str(p['ward']).isdigit() and f['geometry']:
            wards[int(p['ward'])] = ('', transform(tr, shape(f['geometry'])))
    return wards


def load_city_bikes():
    """The CDOT bike route file, unchanged."""
    bikes = []
    for f in json.load(open(RAW / 'bike_routes.geojson'))['features']:
        if not f['geometry']:
            continue
        bikes.append(dict(p=dict(f['properties']), g=transform(tr, shape(f['geometry']))))
    return bikes


def apply_manual(bikes, report):
    """Apply hand-entered changes from data/manual/manual_segments.json.

    The file holds facilities reported in the news but not yet in the CDOT
    file. `report` collects notes on whether each change still applied, so a
    person can remove entries once CDOT catches up.
    """
    M = json.load(open(MANUAL / 'manual_segments.json'))
    pa = Point(*tr(*M['pratt_ashland_lonlat']))
    ref = Point(*tr(-87.6609, 42.0055))
    hit = collections.Counter()
    for b in bikes:
        p = b['p']
        # Pratt: the CDOT row runs Ravenswood -> Sheridan as shared-lane
        # markings; the news-reported protected stretch (added below) covers
        # Ravenswood -> Ashland, so trim this row to Ashland -> Sheridan.
        if p['street'] == 'PRATT BLVD' and p['f_street'] == 'N RAVENSWOOD AVE' and p['t_street'] == 'N SHERIDAN RD':
            g = b['g']
            g = g if g.geom_type == 'LineString' else linemerge(g)
            d = g.project(pa)
            a, z = substring(g, 0, d), substring(g, d, g.length)
            b['g'] = a if a.distance(ref) < z.distance(ref) else z
            p['f_street'] = 'N ASHLAND AVE'
            p['mi_ctrline'] = str(b['g'].length / 5280)
            hit['pratt-trim'] += 1
        for o in M['overrides']:
            if p['street'] == o['street'] and p['f_street'] == o['f_street'] and p['t_street'] == o['t_street']:
                p.update({k: v for k, v in o.items() if k not in ('street', 'f_street', 't_street')})
                hit['override:' + o['street']] += 1
    for f in M['features']:
        bikes.append(dict(p=dict(f['properties']), g=transform(tr, shape(f['geometry']))))
    report['manual_applied'] = dict(hit)
    report['manual_expected'] = ['pratt-trim'] + ['override:' + o['street'] for o in M['overrides']]
    report['manual_features'] = [f['properties']['street'] for f in M['features']]
    return bikes


def roundtrip_lonlat(features):
    """Features built in feet are stored as lon/lat and read back, exactly as
    the original build did (keeps results identical to earlier versions)."""
    out = []
    for f in features:
        out.append(dict(p=dict(f['properties']), g=transform(tr, shape(f['geometry']))))
    return out


def clipped_segments(bikes, U6):
    segs = []
    for i, b in enumerate(bikes):
        g = b['g'].intersection(U6)
        if g.is_empty or g.length < 20:
            continue
        p = b['p']
        segs.append(dict(i=i, g=g, t=p['displayrou'], cf=derived_cf(p), st=p['street'],
                         f=p['f_street'], to=p['t_street'], mi=g.length / 5280))
    return segs


def components(segs, sel, tol=40):
    """Group selected segments into connected pieces (touching within `tol` ft),
    largest first."""
    S = [s for s in segs if sel(s)]
    par = list(range(len(S)))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    tree = STRtree([s['g'] for s in S])
    for a, s in enumerate(S):
        for b in tree.query(s['g'].buffer(tol)):
            if b != a and s['g'].distance(S[b]['g']) <= tol:
                par[find(a)] = find(b)
    C = collections.defaultdict(list)
    for a, s in enumerate(S):
        C[find(a)].append(s)
    return sorted(C.values(), key=lambda c: -sum(x['mi'] for x in c))


# --------------------------------------------------------------------------
# Other layers
# --------------------------------------------------------------------------
def load_streets():
    return json.load(open(RAW / 'streets.geojson'))['features']


def load_layers(wards):
    T = WARDS
    L = {}

    def ward_of(g):
        for w in T:
            if wards[w][1].contains(g):
                return w

    # crashes + people
    c = pd.read_csv(RAW / 'crashes.csv', low_memory=False).dropna(subset=['latitude', 'longitude'])
    c = c[(c.latitude > 41) & (c.longitude < -87)]
    p = pd.read_csv(RAW / 'people.csv')
    p['bp'] = p.person_type.map({'PEDESTRIAN': 'ped', 'BICYCLE': 'bike', 'NON-MOTOR VEHICLE': 'ped'})
    p['ksi'] = p.injury_classification.isin(['FATAL', 'INCAPACITATING INJURY'])
    p['fatal'] = p.injury_classification.eq('FATAL')
    p['inj'] = ~p.injury_classification.isin(['NO INDICATION OF INJURY'])
    pc = p.merge(c[['crash_record_id', 'latitude', 'longitude', 'posted_speed_limit', 'street_name']], on='crash_record_id')
    pc['g'] = [pt(x, y) for x, y in zip(pc.longitude, pc.latitude)]
    pc['ward'] = [ward_of(g) for g in pc.g]
    L.update(crashes=c, people=p, pc=pc, pcw=pc[pc.ward.notna()])

    # L stations + ridership
    s = pd.read_csv(RAW / 'l_stops.csv')
    if 'lat' not in s.columns:
        s[['lat', 'lon']] = s.location.str.extract(r'\(([-\d.]+),\s*([-\d.]+)\)').astype(float)
    st = s.groupby(['map_id', 'station_name'], as_index=False)[['lat', 'lon']].first()
    rid = pd.read_csv(RAW / 'l_ridership.csv')
    rid = rid.rename(columns={c_: 'rides' for c_ in rid.columns if c_.startswith('rides')})
    st = st.merge(rid, left_on='map_id', right_on='station_id', how='left')
    st['g'] = [pt(x, y) for x, y in zip(st.lon, st.lat)]
    st['ward'] = [ward_of(g) for g in st.g]
    L.update(stops=s, stations=st)

    # businesses
    b = pd.read_csv(RAW / 'businesses.csv', low_memory=False).dropna(subset=['latitude', 'longitude'])
    b = b.drop_duplicates(subset=['account_number', 'site_number'])
    b['ward'] = b.ward.astype(int)
    L['biz'] = b

    # census block groups (Cook County)
    shp = next((RAW / 'tiger_bg').glob('*.shp'))
    r = shapefile.Reader(str(shp))
    bgs = []
    for sr, rec in zip(r.shapes(), r.records()):
        d = rec.as_dict()
        if d['COUNTYFP'] != '031':
            continue
        bgs.append((d['GEOID'], transform(tr, shape(sr.__geo_interface__))))
    L['bgs'] = bgs
    a = pd.read_csv(RAW / 'acs_bg.csv', dtype={'GEOID': str}).set_index('GEOID').astype(float)
    L['acs'] = pd.DataFrame({'pop': a.B01003_E001, 'workers': a.B08301_E001, 'transit': a.B08301_E010,
                             'bike': a.B08301_E018, 'walk': a.B08301_E019, 'wfh': a.B08301_E021})
    L['eq'] = pd.DataFrame({'hh': a.B25044_E001, 'zc': a.B25044_E003 + a.B25044_E010,
                            'pu': a.C17002_E001, 'pp': a.C17002_E002 + a.C17002_E003})

    # zoning
    zf = json.load(open(RAW / 'zoning.geojson'))['features']
    L['zones'] = [(f['properties']['zone_class'], transform(tr, shape(f['geometry']))) for f in zf if f['geometry']]
    return L


def zone_family(z):
    return re.match(r'[A-Z]+', z).group()
