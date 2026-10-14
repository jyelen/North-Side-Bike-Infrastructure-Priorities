"""Download every source into data/raw/.

Run:  python pipeline/fetch.py            (all sources)
      python pipeline/fetch.py crashes    (one or more named sources)

Each source writes one file. data/raw/_meta.json records when each source was
fetched and which edition was used (ACS year, IDOT service, bike file date).
"""
import csv
import datetime as dt
import io
import json
import re
import sys
import time
import zipfile

import requests

from config import (CHI, DATASETS, RAW, WARDS, CRASH_START, STATE, COUNTY, ACS_TABLES,
                    IDOT_SERVER, IDOT_LAYER_NAME)

S = requests.Session()
S.headers['User-Agent'] = 'north-side-bike-network-refresh (github.com/jyelen)'
TODAY = dt.date.today()


def log(*a):
    print(*a, flush=True)


def get(url, params=None, tries=4, method='GET', **kw):
    for i in range(tries):
        try:
            if method == 'POST':
                r = S.post(url, data=params, timeout=180, **kw)
            else:
                r = S.get(url, params=params, timeout=180, **kw)
            if r.status_code == 200:
                return r
            msg = f'HTTP {r.status_code}: {r.text[:300]}'
            if r.status_code in (400, 403, 404):
                raise RuntimeError(msg)
        except requests.RequestException as e:
            msg = str(e)
        log(f'   retry {i + 1} after error: {msg}')
        time.sleep(5 * (i + 1))
    raise RuntimeError(f'failed after {tries} tries: {url} {params} :: {msg}')


# --------------------------------------------------------------------------
# Socrata helpers
# --------------------------------------------------------------------------
def view_meta(ds):
    return get(f'{CHI}/api/views/{ds}.json').json()


def geom_column(meta):
    for c in meta['columns']:
        if c.get('dataTypeName') in ('multiline', 'line', 'multipolygon', 'polygon', 'point', 'location', 'multipoint'):
            return c['fieldName']
    raise RuntimeError('no geometry column in ' + meta.get('name', '?') + ': ' +
                       ', '.join(f"{c['fieldName']}:{c.get('dataTypeName')}" for c in meta['columns']) +
                       f" | viewType={meta.get('viewType')} displayType={meta.get('displayType')}")


def col_types(meta):
    return {c['fieldName']: c.get('dataTypeName') for c in meta['columns']}


def soql_in(col, values, dtype):
    if dtype == 'number':
        return f"{col} in({','.join(str(v) for v in values)})"
    return f"{col} in({','.join(repr(str(v)) for v in values)})"


def paged_csv(ds, params, page=50000):
    """All rows of a CSV query, following $offset paging."""
    out, header, off = [], None, 0
    while True:
        p = dict(params, **{'$limit': page, '$offset': off})
        txt = get(f'{CHI}/resource/{ds}.csv', p).text
        rows = list(csv.reader(io.StringIO(txt)))
        if header is None:
            header = rows[0]
        body = rows[1:]
        out += body
        log(f'   {ds}: {len(out)} rows')
        if len(body) < page:
            break
        off += page
    return header, out


def write_csv(path, header, rows):
    with open(path, 'w', newline='') as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(header)
        w.writerows(rows)


def geojson(ds, params=None, page=50000):
    feats, off = [], 0
    while True:
        p = dict(params or {}, **{'$limit': page, '$offset': off, '$order': ':id'})
        fc = get(f'{CHI}/resource/{ds}.geojson', p).json()
        feats += fc['features']
        if len(fc['features']) < page:
            break
        off += page
    return {'type': 'FeatureCollection', 'features': feats}


def _bounds(geoms):
    xs, ys = [], []

    def walk(c):
        if isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)
    for g in geoms:
        walk(g['coordinates'])
    return min(xs), min(ys), max(xs), max(ys)


def ward_box():
    """Bounding box of the six wards, from the freshly downloaded ward file."""
    W = json.load(open(RAW / 'wards.geojson'))['features']
    return _bounds([f['geometry'] for f in W if str(f['properties'].get('ward')) in {str(w) for w in WARDS} and f['geometry']])


def study_box(pad=0.003):
    """Six wards plus the full length of every bike route that enters them
    (routes are measured end to end, so crashes and roads near their far ends
    count too), padded about 1,000 ft."""
    x0, y0, x1, y1 = ward_box()
    feats = json.load(open(RAW / 'bike_routes.geojson'))['features']

    def touches(g):
        bx0, by0, bx1, by1 = _bounds([g])
        return not (bx1 < x0 or bx0 > x1 or by1 < y0 or by0 > y1)
    inside = [f['geometry'] for f in feats if f['geometry'] and touches(f['geometry'])]
    bx0, by0, bx1, by1 = _bounds(inside)
    return min(x0, bx0) - pad, min(y0, by0) - pad, max(x1, bx1) + pad, max(y1, by1) + pad


def within_box(col):
    """Rows whose shape touches the study area. (Socrata's within_box()
    drops shapes that extend past the box, such as the lakefront parks.)"""
    x0, y0, x1, y1 = study_box()
    return f"intersects({col},'POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))')"


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
def f_wards(M):
    fc = geojson(DATASETS['wards'])
    n = sum(1 for f in fc['features'] if str(f['properties'].get('ward')) in {str(w) for w in WARDS})
    assert n == len(WARDS), f'expected {len(WARDS)} target wards, found {n}'
    json.dump(fc, open(RAW / 'wards.geojson', 'w'))
    return {'features': len(fc['features'])}


BIKE_KEYS = ['street', 'st_name', 'oneway_dir', 'f_street', 't_street', 'displayrou', 'mi_ctrline', 'br_oneway', 'br_ow_dir', 'contraflow']


def f_bike_routes(M):
    ds = DATASETS['bike_routes']
    meta = view_meta(ds)
    fc = geojson(ds)
    for f in fc['features']:
        for k in BIKE_KEYS:
            f['properties'].setdefault(k, None)
    missing = [k for k in BIKE_KEYS if all(f['properties'][k] is None for f in fc['features'])]
    assert not missing, f'bike route columns missing: {missing}'
    json.dump(fc, open(RAW / 'bike_routes.geojson', 'w'))
    upd = dt.datetime.fromtimestamp(meta.get('rowsUpdatedAt') or meta['viewLastModified'], dt.timezone.utc).date().isoformat()
    M['bike_routes_updated'] = upd
    return {'features': len(fc['features']), 'rows_updated': upd, 'name': meta.get('name')}


def f_streets(M):
    """Street centerlines. The portal's "Street Center Lines" page is a map
    view (6imu-meau, last changed April 2024) over the transportation dataset
    pr57-gg9e. Read that dataset for the study area; if it cannot be read,
    fall back to the copy saved in data/static/streets.geojson."""
    import shutil
    from config import STATIC
    ds = DATASETS['streets']
    keep = ['pre_dir', 'street_nam', 'street_typ', 'class', 'oneway_dir']
    try:
        g = geom_column(view_meta(ds))
        fc = geojson(ds, {'$where': within_box(g)})
        out = []
        for f in fc['features']:
            if not f.get('geometry'):
                continue
            props = {k.lower(): v for k, v in f['properties'].items()}
            out.append({'type': 'Feature', 'properties': {k: props.get(k) for k in keep}, 'geometry': f['geometry']})
        if not out or not any(o['properties']['class'] for o in out):
            raise RuntimeError('no usable street features (fields: ' + ', '.join(sorted(fc['features'][0]['properties'])) + ')'
                               if fc['features'] else 'no street features returned')
        json.dump({'type': 'FeatureCollection', 'features': out}, open(RAW / 'streets.geojson', 'w'))
        M['streets_source'] = ds
        return {'features': len(out)}
    except Exception as e:
        log(f'   live street data unavailable ({e}); using saved copy')
        shutil.copy(STATIC / 'streets.geojson', RAW / 'streets.geojson')
        M['streets_source'] = 'saved copy (data/static/streets.geojson)'
        return {'features': len(json.load(open(RAW / 'streets.geojson'))['features']), 'source': 'saved copy',
                'live_error': str(e)[:300]}


def f_zoning(M):
    ds = DATASETS['zoning']
    g = geom_column(view_meta(ds))
    fc = geojson(ds, {'$select': f'{g},zone_class,zone_type', '$where': within_box(g)})
    json.dump(fc, open(RAW / 'zoning.geojson', 'w'))
    return {'features': len(fc['features'])}


CRASH_COLS = ['crash_record_id', 'crash_date', 'posted_speed_limit', 'first_crash_type', 'most_severe_injury', 'injuries_total',
              'injuries_fatal', 'injuries_incapacitating', 'street_no', 'street_direction', 'street_name', 'latitude', 'longitude']


def f_crashes(M):
    x0, y0, x1, y1 = study_box()
    where = (f"crash_date>='{CRASH_START}T00:00:00' AND latitude between {y0} and {y1} "
             f"AND longitude between {x0} and {x1}")
    h, rows = paged_csv(DATASETS['crashes'], {'$select': ','.join(CRASH_COLS), '$where': where, '$order': 'crash_record_id'})
    write_csv(RAW / 'crashes.csv', h, rows)
    return {'rows': len(rows)}


def f_people(M):
    where = (f"person_type in('PEDESTRIAN','BICYCLE','NON-MOTOR VEHICLE') AND crash_date>='{CRASH_START}T00:00:00'")
    h, rows = paged_csv(DATASETS['people'], {'$select': 'crash_record_id,person_id,person_type,crash_date,injury_classification',
                                             '$where': where, '$order': 'person_id'})
    write_csv(RAW / 'people.csv', h, rows)
    return {'rows': len(rows)}


def f_businesses(M):
    ds = DATASETS['businesses']
    types = col_types(view_meta(ds))
    h, rows = paged_csv(ds, {'$where': soql_in('ward', WARDS, types.get('ward')), '$order': 'id'})
    write_csv(RAW / 'businesses.csv', h, rows)
    return {'rows': len(rows)}


def f_l_stops(M):
    rows = get(f'{CHI}/resource/{DATASETS["l_stops"]}.json', {'$limit': 5000}).json()
    keys = ['stop_id', 'direction_id', 'stop_name', 'station_name', 'station_descriptive_name', 'map_id', 'ada',
            'red', 'blue', 'g', 'brn', 'p', 'y', 'pnk', 'o', 'lat', 'lon']
    out = []
    for r in rows:
        loc = r.get('location') or {}
        if 'coordinates' in loc:
            lon, lat = loc['coordinates']
        else:
            lat, lon = loc.get('latitude'), loc.get('longitude')
        r['lat'], r['lon'] = float(lat), float(lon)
        out.append([str(r.get(k)).lower() if isinstance(r.get(k), bool) else r.get(k) for k in keys])
    write_csv(RAW / 'l_stops.csv', keys, out)
    return {'rows': len(out)}


def f_l_ridership(M):
    year = TODAY.year - 1
    q = {'$select': 'station_id,max(stationname) as stationname,sum(rides) as rides',
         '$where': f"date between '{year}-01-01T00:00:00' and '{year}-12-31T23:59:59'",
         '$group': 'station_id', '$limit': 5000}
    h, rows = paged_csv(DATASETS['l_ridership'], q)
    write_csv(RAW / 'l_ridership.csv', h, rows)
    M['ridership_year'] = year
    return {'rows': len(rows), 'year': year}


def acs_summary_file(year, table):
    """Rows for Cook County block groups from the ACS table-based summary file
    (no API key needed). Streams the national file and keeps matching lines."""
    url = (f'https://www2.census.gov/programs-surveys/acs/summary_file/{year}/table-based-SF/data/'
           f'5YRData/acsdt5y{year}-{table.lower()}.dat')
    prefix = f'1500000US{STATE}{COUNTY}'
    with S.get(url, stream=True, timeout=600) as r:
        if r.status_code != 200:
            return None
        r.encoding = 'utf-8'
        lines = r.iter_lines(decode_unicode=True)
        head = next(lines).split('|')
        rows = [l.split('|') for l in lines if l.startswith(prefix)]
    return head, rows


def acs_api(year, key):
    api_vars = list(ACS_TABLES.values())
    r = S.get(f'https://api.census.gov/data/{year}/acs/acs5',
              params={'get': 'GEO_ID,' + ','.join(api_vars), 'for': 'block group:*',
                      'in': [f'state:{STATE}', f'county:{COUNTY}', 'tract:*'], 'key': key}, timeout=180)
    if r.status_code != 200 or not r.text.startswith('['):
        return None
    data = r.json()
    inv_ = {v: k for k, v in ACS_TABLES.items()}
    return {row[0][-12:]: {inv_[h]: row[i] for i, h in enumerate(data[0]) if h in inv_} for row in data[1:]}


def f_acs(M):
    """ACS 5-year estimates by block group. Uses the Census API if a key is set
    (CENSUS_API_KEY), otherwise the downloadable summary files."""
    import os
    names = list(ACS_TABLES)
    tables = sorted({n.split('_')[0] for n in names})
    key = os.environ.get('CENSUS_API_KEY')
    vals, year, how = None, None, None
    for y in range(TODAY.year - 1, TODAY.year - 4, -1):
        if key:
            vals = acs_api(y, key)
            if vals:
                year, how = y, 'Census API'
                break
        got = {}
        for tb in tables:
            res = acs_summary_file(y, tb)
            if res is None:
                break
            head, rows = res
            gi = head.index('GEO_ID')
            for row in rows:
                d = got.setdefault(row[gi][-12:], {})
                for n in names:
                    if n.startswith(tb + '_'):
                        d[n] = row[head.index(n)]
        else:
            vals, year, how = got, y, 'summary files'
            break
        log(f'   ACS {y}: not published yet')
    assert vals, 'no ACS 5-year release found'
    jam = 0
    out = []
    for gid in sorted(vals):
        row = []
        for n in names:
            x = vals[gid].get(n)
            try:
                x = float(x)
            except (TypeError, ValueError):
                x = -1
            if x < 0:          # Census "not available" markers
                jam += 1
                x = 0.0
            row.append(x)
        out.append([gid] + row)
    write_csv(RAW / 'acs_bg.csv', ['GEOID'] + names, out)
    M['acs_year'] = year
    # block group shapes for the same year
    tdir = RAW / 'tiger_bg'
    tdir.mkdir(exist_ok=True)
    for old in tdir.glob('*'):
        old.unlink()
    z = get(f'https://www2.census.gov/geo/tiger/TIGER{year}/BG/tl_{year}_{STATE}_bg.zip').content
    zipfile.ZipFile(io.BytesIO(z)).extractall(tdir)
    return {'block_groups': len(out), 'year': year, 'via': how, 'not_available_values': jam}


IDOT_FIELDS = ['INVENTORY', 'BEG_STA', 'END_STA', 'AADT', 'AADT_YR', 'FC', 'I_SHD1_TYP', 'I_SHD1_WTH', 'JUR_1', 'JUR_TYPE',
               'KEY_RT_TYP', 'LN_WTH', 'LNS', 'MARKED_RT', 'MED_TYP', 'MED_WTH', 'MRK_RT_TYP', 'MUNI', 'NHS', 'O_SHD1_TYP',
               'O_SHD1_WTH', 'OP_1_2_WAY', 'PRK_LT', 'PRK_RT', 'ROAD_NAME', 'ROW', 'ROW_AVL', 'SEG_LENGTH', 'SP_LIM',
               'SURF_TYP', 'SURF_WTH', 'URBAN']
IDOT_REQUIRED = ['FC', 'JUR_TYPE', 'LNS', 'SURF_WTH', 'PRK_LT', 'PRK_RT', 'SP_LIM', 'AADT', 'AADT_YR', 'OP_1_2_WAY', 'MED_TYP', 'ROAD_NAME']


def f_idot(M):
    root = get(IDOT_SERVER, {'f': 'json'}).json()
    cands = [(s['name'], s['type']) for s in root.get('services', [])]
    for folder in root.get('folders', []):
        try:
            sub = get(f'{IDOT_SERVER}/{folder}', {'f': 'json'}, tries=2).json()
            cands += [(s['name'], s['type']) for s in sub.get('services', [])]
        except RuntimeError:
            pass
    tools = sorted([(int(re.search(r'(\d{4})$', n).group(1)), n, t) for n, t in cands
                    if re.search(r'cis_LocationTool\d{4}$', n) and t == 'MapServer'])
    assert tools, 'IDOT cis_LocationTool service not found'
    log('   IDOT candidates:', [n for _, n, _ in tools])
    _, name, typ = tools[-1]
    base = f'{IDOT_SERVER}/{name}/{typ}'
    info = get(base, {'f': 'json'}).json()
    layer = next(l for l in info['layers'] if l['name'].strip().upper() == IDOT_LAYER_NAME)
    lurl = f"{base}/{layer['id']}"
    linfo = get(lurl, {'f': 'json'}).json()
    page = min(linfo.get('maxRecordCount') or 1000, 1000)
    x0, y0, x1, y1 = study_box()
    box = {'where': '1=1', 'geometry': f'{x0},{y0},{x1},{y1}', 'geometryType': 'esriGeometryEnvelope', 'inSR': 4326,
           'spatialRel': 'esriSpatialRelIntersects', 'f': 'json'}
    total = get(f'{lurl}/query', dict(box, returnCountOnly='true')).json().get('count')
    have = {f['name'] for f in linfo['fields']}
    fields = [f for f in IDOT_FIELDS if f in have]
    missing = [f for f in IDOT_FIELDS if f not in have]
    assert not [f for f in missing if f in IDOT_REQUIRED], f'IDOT fields missing: {missing}'
    # Note: this layer's object ids are generated per request, so page by a
    # stored id column instead (asking for named outFields fails here, so take
    # all fields and trim).
    order = 'OBJECTID' if 'OBJECTID' in have else 'INVENTORY,BEG_STA,END_STA'
    feats, off = [], 0
    while True:
        r = get(f'{lurl}/query', dict(box, outFields='*', outSR=4326, returnGeometry='true', orderByFields=order,
                                      resultOffset=off, resultRecordCount=page)).json()
        if 'error' in r:
            raise RuntimeError(f"IDOT query error: {r['error']}")
        got = r.get('features', [])
        for f in got:
            paths = (f.get('geometry') or {}).get('paths')
            # keep x, y only
            paths = [[c[:2] for c in path] for path in paths] if paths else None
            geom = None if not paths else ({'type': 'LineString', 'coordinates': paths[0]} if len(paths) == 1
                                           else {'type': 'MultiLineString', 'coordinates': paths})
            feats.append({'type': 'Feature', 'properties': {k: f['attributes'].get(k) for k in fields},
                          'geometry': geom, '_oid': f['attributes'].get('OBJECTID')})
        if not got or (not r.get('exceededTransferLimit') and len(got) < page):
            break
        off += page
    oids = [f.pop('_oid') for f in feats]
    log(f'   IDOT: {len(feats)} records (server count {total}, distinct ids {len(set(oids))})')
    assert total is None or len(feats) == total, 'IDOT record count does not match the server count'
    assert None in oids or len(set(oids)) == len(oids), 'IDOT paging returned duplicates'
    fc = {'type': 'FeatureCollection', 'source': f"IDOT {name} {typ}/{layer['id']} {IDOT_LAYER_NAME}",
          'fetched': dt.datetime.now(dt.timezone.utc).isoformat(), 'features': feats}
    json.dump(fc, open(RAW / 'idot_roads.geojson', 'w'))
    M['idot_service'] = name
    return {'features': len(feats), 'service': name}


# wards and bike routes must come first: other queries use their bounding box
SOURCES = {'wards': f_wards, 'bike_routes': f_bike_routes, 'streets': f_streets, 'zoning': f_zoning,
           'crashes': f_crashes, 'people': f_people, 'businesses': f_businesses, 'l_stops': f_l_stops,
           'l_ridership': f_l_ridership, 'acs': f_acs, 'idot': f_idot}


def main(names):
    RAW.mkdir(parents=True, exist_ok=True)
    mp = RAW / '_meta.json'
    M = json.load(open(mp)) if mp.exists() else {}
    M.pop('baseline', None)
    M.setdefault('fetched', {})
    M.setdefault('counts', {})
    failed = []
    for n in names or list(SOURCES):
        log(f'-- {n}')
        try:
            info = SOURCES[n](M)
            M['fetched'][n] = TODAY.isoformat()
            M['counts'][n] = info
            log('   ok', info)
        except Exception as e:  # keep going so one broken source shows every problem at once
            log(f'   FAILED: {e}')
            failed.append(n)
    M['fetched']['idot'] = M['fetched'].get('idot', TODAY.isoformat())
    json.dump(M, open(mp, 'w'), indent=1)
    if failed:
        log('FAILED SOURCES:', ', '.join(failed))
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv[1:])
