"""Turn the raw downloads into data/build/bikenet_data.json, the data file
embedded in the page.

Steps, in order:
  1. bike routes  = CDOT file + hand-entered news changes + major streets with no facility
  2. ward summary table (crashes, transit, census, businesses, zoning per ward)
  3. per-segment measures (injuries, distance to L, density, zoning, links)
  4. map layers (wards, streets, crashes, stations, block groups, zoning, businesses)
  5. segment history against archived city snapshots
  6. traffic stress, low-stress links, car ownership and poverty
  7. IDOT road measurements (width, lanes, parking, traffic, speed, owner)
  8. headline numbers (citywide comparison, network pieces, miles with no facility)
"""
import collections
import json
import re
import sys

import pandas as pd
from shapely.geometry import shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from config import BUILD, RAW, STATIC, WARDS, MANUAL_SOURCE_DATE
from load import (SQMI, tr, inv, pt, derived_cf, load_wards, load_city_bikes, apply_manual,
                  roundtrip_lonlat, clipped_segments, components, load_streets, load_layers)
from unserved import build_unserved
from trails import build_trails

T = WARDS
TYPE = {'Protected Bike Lane': 'P', 'Buffered Bike Lane': 'BF', 'Bike Lane': 'L', 'Marked Shared Lane': 'S',
        'Neighborhood Greenway': 'G', 'No bike facility': 'N', 'Off-Street Trail': 'T'}
LOW_STRESS = ('Protected Bike Lane', 'Neighborhood Greenway', 'Off-Street Trail')


def log(*a):
    print(*a, flush=True)


# --------------------------------------------------------------------------
# geometry helpers for the output file
# --------------------------------------------------------------------------
def ll(g, tol=0):
    g = g.simplify(tol) if tol else g
    return transform(inv, g)


def coords(g):
    """Compact coordinate arrays (lat, lng rounded to 5 places)."""
    def r(seq):
        return [[round(y, 5), round(x, 5)] for x, y in seq]
    t = g.geom_type
    if t == 'LineString':
        return [r(g.coords)]
    if t == 'MultiLineString':
        return [r(l.coords) for l in g.geoms]
    if t == 'Polygon':
        return [[r(g.exterior.coords)] + [r(i.coords) for i in g.interiors]]
    if t == 'MultiPolygon':
        return [[r(p.exterior.coords)] + [r(i.coords) for i in p.interiors] for p in g.geoms]
    if t == 'GeometryCollection':
        out = []
        for x in g.geoms:
            if x.geom_type in ('LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'):
                out += coords(x)
        return out
    return []


# --------------------------------------------------------------------------
# 2. ward summary
# --------------------------------------------------------------------------
def ward_summary(wards, L):
    pcw, st, bgs, acs, b, zones = L['pcw'], L['stations'], L['bgs'], L['acs'], L['biz'], L['zones']
    rows = {}
    for w in T:
        W = wards[w][1]
        a = W.area / SQMI
        R = {}
        d = pcw[pcw.ward == w]
        R['Area (sq mi)'] = a
        R['Pedestrians injured since 2021'] = (d.inj & (d.bp == 'ped')).sum()
        R['Cyclists injured since 2021'] = (d.inj & (d.bp == 'bike')).sum()
        R['Killed or seriously injured'] = d.ksi.sum()
        R['  of which killed'] = d.fatal.sum()
        R['% of injuries where posted limit ≥30'] = 100 * (d[d.inj].posted_speed_limit >= 30).mean()
        ss = st[st.ward == w]
        R['L stations'] = len(ss)
        R['L entries last full year (millions)'] = ss.rides.sum() / 1e6
        agg = collections.defaultdict(float)
        for gid, g in bgs:
            if not g.intersects(W) or gid not in acs.index:
                continue
            f = g.intersection(W).area / g.area
            for k, v in acs.loc[gid].items():
                agg[k] += f * v
        R['Population'] = agg['pop']
        R['Pop per sq mi'] = agg['pop'] / a
        R['% commute by bike'] = 100 * agg['bike'] / agg['workers']
        R['% walk'] = 100 * agg['walk'] / agg['workers']
        R['% transit'] = 100 * agg['transit'] / agg['workers']
        R['Licensed businesses'] = (b.ward == w).sum()
        R['Businesses per sq mi'] = R['Licensed businesses'] / a
        za = collections.defaultdict(float)
        for zc, g in zones:
            if g.intersects(W):
                za[re.match(r'[A-Z]+', zc).group()] += g.intersection(W).area
        R['% zoned business/commercial'] = 100 * (za['B'] + za['C']) / sum(za.values())
        R['_agg'] = agg
        rows[w] = R
    keys = [k for k in rows[T[0]] if k != '_agg']
    df = pd.DataFrame({w: {k: rows[w][k] for k in keys} for w in T})
    tot = {k: sum(rows[w][k] for w in T) for k in keys}
    A = collections.defaultdict(float)
    for w in T:
        for k, v in rows[w]['_agg'].items():
            A[k] += v
    allc = {k: tot[k] for k in keys}
    allc['Pop per sq mi'] = tot['Population'] / tot['Area (sq mi)']
    allc['Businesses per sq mi'] = tot['Licensed businesses'] / tot['Area (sq mi)']
    d = pcw
    allc['% of injuries where posted limit ≥30'] = 100 * (d[d.inj].posted_speed_limit >= 30).mean()
    allc['% commute by bike'] = 100 * A['bike'] / A['workers']
    allc['% walk'] = 100 * A['walk'] / A['workers']
    allc['% transit'] = 100 * A['transit'] / A['workers']
    allc['% zoned business/commercial'] = None
    df['All'] = pd.Series(allc)
    # the original build wrote this table to CSV and read it back; do the same
    # so numbers (and their float rounding) are identical
    path = BUILD / 'ward_layer_summary.csv'
    df.to_csv(path)
    return pd.read_csv(path, index_col=0)


def city_comparison(wards, L):
    """Citywide numbers for the 'compared with Chicago' lines."""
    bgs, acs, p = L['bgs'], L['acs'], L['people']
    CITY = unary_union([g for _, g in wards.values()])
    city = collections.defaultdict(float)
    for gid, g in bgs:
        if gid in acs.index and g.representative_point().within(CITY):
            for k, v in acs.loc[gid].items():
                city[k] += v
    area = CITY.area / SQMI
    return {'area': round(area, 1), 'pop': int(round(city['pop'])), 'den': int(round(city['pop'] / area)),
            'bk': round(100 * city['bike'] / city['workers'], 1), 'wk': round(100 * city['walk'] / city['workers'], 1),
            'tr': round(100 * city['transit'] / city['workers'], 1),
            'ksi': int(p.ksi.sum()), 'killed': int(p.fatal.sum())}


# --------------------------------------------------------------------------
# 3 + 4. segments and map layers
# --------------------------------------------------------------------------
def segments_and_layers(bikes, wards, U6, segs, L, summ):
    UN = U6
    OUT = {}
    CRASH = L['crashes'].copy()
    pc, st, b, bgs, acs, zones, s = L['pc'], L['stations'].copy(), L['biz'].copy(), L['bgs'], L['acs'], L['zones'], L['stops']
    OUT['wards'] = [{'w': w, 'c': coords(ll(wards[w][1], 25)),
                     'lab': [round(v, 5) for v in list(ll(wards[w][1].representative_point()).coords)[0][::-1]]} for w in T]
    # streets basemap
    sf = load_streets()
    NB = UN.buffer(1500)
    streets = []
    for f in sf:
        p = f['properties']
        if not f['geometry'] or p.get('class') not in ('1', '2', '3', '4', 'E', 'RIV'):
            continue
        g = transform(tr, shape(f['geometry']))
        if not g.intersects(NB):
            continue
        nm = ' '.join(x for x in [p.get('pre_dir'), p.get('street_nam'), p.get('street_typ')] if x)
        streets.append((p['class'], nm.title(), p.get('oneway_dir') or '', g))
    grp = collections.defaultdict(list)
    for c, n, o, g in streets:
        grp[(c, n)].append(g)
    OUT['streets'] = [{'k': c, 'n': n, 'c': coords(ll(unary_union(gs), 12))} for (c, n), gs in grp.items()]
    ow = [g for c, n, o, g in streets if o in ('N', 'S', 'E', 'W') and c in ('1', '2', '3', '4')]
    owU = unary_union(ow).buffer(20) if ow else None
    # lookups
    crash_pts = CRASH.copy()
    crash_pts['g'] = [pt(x, y) for x, y in zip(crash_pts.longitude, crash_pts.latitude)]
    ctree = STRtree(list(crash_pts.g))
    cspeed = crash_pts.posted_speed_limit.values
    inj = pc[pc.inj].reset_index(drop=True)
    itree = STRtree(list(inj.g))
    stg = list(st.g)
    stree = STRtree(stg)
    b['g'] = [pt(x, y) for x, y in zip(b.longitude, b.latitude)]
    btree = STRtree(list(b.g))
    bgg = [g for _, g in bgs]
    bgid = [i for i, _ in bgs]
    bgtree = STRtree(bgg)
    zg = [g for _, g in zones]
    zc = [z for z, _ in zones]
    ztree = STRtree(zg)
    PC = components(segs, lambda s_: s_['t'] == 'Protected Bike Lane' or s_['cf'])
    comp_geoms = [unary_union([x['g'] for x in cc]) for cc in PC]
    comp_tree = STRtree(comp_geoms)
    segout = []
    for i, bk in enumerate(bikes):
        G = bk['g']
        if not G.intersects(UN):
            continue
        inside = G.intersection(UN)
        if inside.length < 30:
            continue
        p = bk['p']
        g = G
        Lm = g.length / 5280
        wl = {w: G.intersection(wards[w][1]).length for w in T}
        W = max(wl, key=wl.get)
        buf = g.buffer(100)
        ii = [k for k in itree.query(buf) if buf.contains(inj.g[k])]
        sub = inj.iloc[ii]
        cc = [k for k in ctree.query(g.buffer(75)) if g.buffer(75).contains(crash_pts.g.iloc[k])]
        sp = pd.Series(cspeed[cc])
        sp = sp[sp > 0]
        speed = int(sp.mode().iloc[0]) if len(sp) >= 5 else None
        rail = min(g.distance(stg[k]) for k in stree.query(g.buffer(30000))) / 5280
        b3 = g.buffer(300)
        biz = sum(1 for k in btree.query(b3) if b3.contains(b.g.iloc[k]))
        q = g.buffer(1320)
        agg = collections.defaultdict(float)
        for k in bgtree.query(q):
            if bgid[k] not in acs.index:
                continue
            f = bgg[k].intersection(q).area / bgg[k].area
            for kk, v in acs.loc[bgid[k]].items():
                agg[kk] += f * v
        lakefix = sum(bgg[k].intersection(q).area for k in bgtree.query(q)) / q.area
        dens = agg['pop'] / (q.area * max(lakefix, 0.05) / SQMI)
        zb = g.buffer(100)
        za = collections.defaultdict(float)
        for k in ztree.query(zb):
            a = zg[k].intersection(zb).area
            if a:
                za[re.match(r'[A-Z]+', zc[k]).group()] += a
        zt = sum(za.values()) or 1
        t = TYPE[p['displayrou']]
        cf = derived_cf(p)
        links = None
        if not (t == 'P' or cf):
            tb = g.buffer(40)
            links = len({k for k in comp_tree.query(tb) if comp_geoms[k].intersects(tb)})
        oneway = round(G.intersection(owU).length / G.length, 2) if owU is not None else 0
        segout.append({'id': i, 'st': p['street'].title(), 'f': (p['f_street'] or '').title(), 'to': (p['t_street'] or '').title(),
                       't': t, 'cf': int(cf), 'mi': round(Lm, 3), 'w': W,
                       'bi': int((sub.bp == 'bike').sum()), 'pi': int((sub.bp == 'ped').sum()), 'ksi': int(sub.ksi.sum()), 'k': int(sub.fatal.sum()),
                       'sp': speed, 'rail': round(rail, 2), 'biz': biz, 'den': round(dens),
                       'bk': round(100 * agg['bike'] / agg['workers'], 1) if agg['workers'] else None,
                       'tr': round(100 * agg['transit'] / agg['workers'], 1) if agg['workers'] else None,
                       'com': round(100 * (za['B'] + za['C']) / zt), 'lnk': links, 'ow': oneway,
                       'src': p.get('src'), 'note': p.get('note'), 'url': p.get('src_url'), 'c': coords(ll(G, 4))})
    OUT['segs'] = segout
    log('  segments:', len(segout))
    # crashes placeholder (replaced with the detailed version in step 5)
    OUT['crashes'] = []
    # stations
    LINES = {'red': 'Red', 'blue': 'Blue', 'g': 'Green', 'brn': 'Brown', 'p': 'Purple', 'y': 'Yellow', 'pnk': 'Pink', 'o': 'Orange'}
    s2 = s.copy()
    s2['lines'] = s2.apply(lambda r: [v for k, v in LINES.items() if str(r[k]).lower() == 'true'], axis=1)
    ln = s2.groupby('map_id')['lines'].apply(lambda x: sorted({l for ls in x for l in ls}))
    NB5 = UN.buffer(2640)
    OUT['stations'] = [{'n': r.station_name, 'll': [round(r.lat, 5), round(r.lon, 5)], 'r': int(r.rides) if pd.notna(r.rides) else None,
                        'l': ln.get(r.map_id, []), 'w': int(r.ward) if pd.notna(r.ward) else None}
                       for r in st.itertuples() if NB5.contains(r.g)]
    # block groups
    bgo = []
    for gid, g in bgs:
        if not g.intersects(UN) or gid not in acs.index:
            continue
        gi = g.intersection(UN)
        if gi.area < 0.01 * g.area:
            continue
        a = acs.loc[gid]
        ar = g.area / SQMI
        bgo.append({'id': gid, 'c': coords(ll(gi, 20)), 'pop': int(a['pop']), 'den': round(a['pop'] / ar), 'w': int(a['workers']),
                    'bk': round(100 * a['bike'] / a['workers'], 1) if a['workers'] else None,
                    'wk': round(100 * a['walk'] / a['workers'], 1) if a['workers'] else None,
                    'tr': round(100 * a['transit'] / a['workers'], 1) if a['workers'] else None})
    OUT['bgs'] = bgo
    # zoning dissolved by class
    zgrp = collections.defaultdict(list)
    for z, g in zones:
        gi = g.intersection(UN)
        if not gi.is_empty and gi.area > 100:
            zgrp[z].append(gi)
    OUT['zoning'] = [{'z': z, 'c': coords(ll(unary_union(gs).buffer(0), 15))} for z, gs in zgrp.items()]
    OUT['biz'] = []  # replaced in step 5
    # ward summary + facility miles
    fac = collections.defaultdict(lambda: collections.defaultdict(float))
    for s_ in segs:
        k = 'CF' if (s_['cf'] and s_['t'] != 'Protected Bike Lane') else TYPE[s_['t']]
        for w in T:
            l_ = s_['g'].intersection(wards[w][1]).length / 5280
            if l_ > 0:
                fac[w][k] += l_
    OUT['wardsum'] = {str(w): {**{k: (None if pd.isna(v) else float(v)) for k, v in summ[str(w)].items()},
                               'fac': {k: round(v, 2) for k, v in fac[w].items()}} for w in T}
    OUT['wardsum']['all'] = {**{k: (None if pd.isna(v) else float(v)) for k, v in summ['All'].items()},
                             'fac': {k: round(sum(fac[w].get(k, 0) for w in T), 2) for k in ['P', 'CF', 'G', 'BF', 'L', 'S', 'T']}}
    OUT['meta'] = {'crash_max': str(CRASH.crash_date.max())[:10]}
    return OUT


# --------------------------------------------------------------------------
# 5. history, crash detail, business detail
# --------------------------------------------------------------------------
SNAP = [('2014-12', 'Dec 2014'), ('2016-03', 'Mar 2016'), ('2018-11', 'Nov 2018'), ('2020-02', 'Feb 2020'),
        ('2021-01', 'Jan 2021'), ('2021-11', 'Nov 2021'), ('2022-12', 'Dec 2022')]


def snap_cat(lbl):
    l = (lbl or '').upper()
    if 'RECOMMENDED' in l or 'TRAIL' in l or 'ACCESS' in l or not l:
        return None
    if 'PROTECTED' in l or 'CYCLE TRACK' in l:
        return 'P'
    if 'BUFFERED' in l:
        return 'BF'
    if 'GREENWAY' in l:
        return 'G'
    if 'SHARED' in l:
        return 'S'
    if 'BIKE LANE' in l:
        return 'L'
    return None


def history_and_detail(D, bikes, U6, L, current_label):
    H = json.load(open(STATIC / 'bike_route_snapshots.json'))
    snaps = {}
    for key, lab in SNAP:
        feats = []
        for k, t in H.items():
            if k.startswith(key):
                for f in json.loads(t)['features']:
                    p = f['properties']
                    c_ = snap_cat(p.get('displayrou') or p.get('bikeroute'))
                    if c_ and f['geometry']:
                        feats.append((c_, transform(tr, shape(f['geometry']))))
        geoms = [g for _, g in feats]
        snaps[key] = (feats, STRtree(geoms), geoms)

    def sample(g, step=60):
        n = max(int(g.length // step), 1)
        return [g.interpolate(i * g.length / n) for i in range(n + 1)]

    def status(g, key):
        feats, tree, geoms = snaps[key]
        pts = sample(g)
        types = collections.Counter()
        cov = 0
        for p in pts:
            hit = [feats[k][0] for k in tree.query(p.buffer(45)) if geoms[k].distance(p) <= 45]
            if hit:
                cov += 1
                types[sorted(hit, key=['P', 'BF', 'L', 'G', 'S'].index)[0]] += 1
        return cov / len(pts), (types.most_common(1)[0][0] if types else None)

    lab = dict(SNAP)
    for s in D['segs']:
        g = bikes[s['id']]['g']
        first = firstType = None
        hist = []
        want = s['t']
        for key, lb in SNAP:
            frac, ty = status(g, key)
            on = frac >= 0.6
            hist.append([lb, ty if on else None])
            if on and first is None:
                first = key
            if on and ty == want and firstType is None:
                firstType = key

        def phrase(k):
            if k is None:
                return f'between Dec 2022 and {current_label}'
            i = [x for x, _ in SNAP].index(k)
            return f'by {lab[k]} (earliest snapshot)' if i == 0 else f'between {SNAP[i-1][1]} and {lab[k]}'
        s['add'] = phrase(first)
        s['typ'] = phrase(firstType)
        s['hist'] = hist
        if s['t'] == 'N':
            s['add'] = 'never (no bike facility on the city map)'
            s['typ'] = s['add']
        elif s.get('src') == 'news':
            s['add'] = f'after {current_label} (news report; not yet in CDOT data)'
            s['typ'] = s['add']
        elif s.get('src') == 'news-unconfirmed':
            s['typ'] = f'after {current_label}, reported but not confirmed complete'

    UN = U6
    cr_cols = pd.read_csv(RAW / 'crashes.csv', low_memory=False)[['crash_record_id', 'crash_date', 'street_no', 'street_direction', 'street_name',
                                                                 'first_crash_type', 'posted_speed_limit', 'latitude', 'longitude']]
    pp = pd.read_csv(RAW / 'people.csv').merge(cr_cols, on='crash_record_id', suffixes=('_p', ''))
    pp = pp.dropna(subset=['latitude', 'longitude'])
    pp = pp[pp.latitude > 41]
    pp = pp[~pp.injury_classification.eq('NO INDICATION OF INJURY')]
    NB3 = UN.buffer(300)
    out = []
    for r in pp.itertuples():
        g = pt(r.longitude, r.latitude)
        if not NB3.contains(g):
            continue
        sev = 'K' if r.injury_classification == 'FATAL' else ('A' if r.injury_classification == 'INCAPACITATING INJURY' else 'I')
        addr = ' '.join(str(x) for x in [int(r.street_no) if pd.notna(r.street_no) else '', r.street_direction if pd.notna(r.street_direction) else '',
                                         str(r.street_name).title()] if x != '')
        out.append([round(r.latitude, 5), round(r.longitude, 5), 'b' if r.person_type == 'BICYCLE' else 'p', sev, str(r.crash_date)[:16], addr,
                    int(r.posted_speed_limit) if pd.notna(r.posted_speed_limit) else None, str(r.injury_classification).capitalize(),
                    str(r.first_crash_type).capitalize(), str(r.person_type).capitalize()])
    D['crashes'] = out
    b = L['biz']
    bb = b[b.ward.isin(T)]
    D['biz'] = [[round(r.latitude, 5), round(r.longitude, 5),
                 str(r.doing_business_as_name if pd.notna(r.doing_business_as_name) else r.legal_name).title()[:60],
                 str(r.license_description).title()[:50], str(r.address).title(), int(r.ward)] for r in bb.itertuples()]
    bpts = [pt(x, y) for x, y in zip(bb.longitude, bb.latitude)]
    btree2 = STRtree(bpts)
    bgmap = {gid: g for gid, g in L['bgs']}
    for o in D['bgs']:
        g = bgmap[o['id']]
        gi = g.intersection(UN)
        n = sum(1 for k in btree2.query(gi) if gi.contains(bpts[k]))
        area = gi.area / SQMI
        popc = o['pop'] * gi.area / g.area
        o['bz'] = n
        o['bzd'] = round(n / area) if area > 0 else None
        o['bzp'] = round(1000 * n / popc, 1) if popc >= 150 else None
        o['popc'] = round(popc)


# --------------------------------------------------------------------------
# 6. traffic stress, low-stress links, equity
# --------------------------------------------------------------------------
def stress_and_equity(D, bikes, segs, L, U6=None):
    eq = L['eq']
    sf = load_streets()
    art, col = [], []
    for f in sf:
        k = f['properties'].get('class')
        if not f['geometry']:
            continue
        g = transform(tr, shape(f['geometry']))
        if k in ('1', '2'):
            art.append(g)
        elif k == '3':
            col.append(g)
    ARTb = unary_union(art).buffer(35)
    COLb = unary_union(col).buffer(35)
    LS = components(segs, lambda s: s['t'] in LOW_STRESS or s['cf'])
    lsg = [unary_union([x['g'] for x in cc]) for cc in LS]
    lst = STRtree(lsg)
    bgs = L['bgs']
    bgg = [g for _, g in bgs]
    bgid = [i for i, _ in bgs]
    bgtree = STRtree(bgg)
    for s in D['segs']:
        g = bikes[s['id']]['g']
        Lg = g.length
        a = g.intersection(ARTb).length / Lg
        c = g.intersection(COLb).length / Lg
        s['art'] = round(min(a, 1), 2)
        s['col'] = round(min(max(c - a, 0), 1), 2)
        s['stress'] = round(min(1, a) + 0.75 * min(max(c - a, 0), 1), 2)
        if s['t'] in ('P', 'G') or s['cf']:
            s['lnk2'] = None
        else:
            tb = g.buffer(40)
            hit = sorted({int(k) for k in lst.query(tb) if lsg[k].intersects(tb)})
            s['lnk2'] = len(hit)
            s['lsc'] = hit          # which low-stress pieces it touches (for the simple view's network math)
        q = g.buffer(1320)
        A = collections.defaultdict(float)
        for k in bgtree.query(q):
            if bgid[k] not in eq.index:
                continue
            f = bgg[k].intersection(q).area / bgg[k].area
            for kk, v in eq.loc[bgid[k]].items():
                A[kk] += f * v
        s['zc'] = round(100 * A['zc'] / A['hh'], 1) if A['hh'] else None
        s['pov'] = round(100 * A['pp'] / A['pu'], 1) if A['pu'] else None
    # low-stress pieces (largest first) and which candidate segments touch each other,
    # so the page can work out how upgrades would join the pieces
    D['meta']['lscomp'] = [round(sum(x['mi'] for x in cc), 3) for cc in LS]
    cand = [s for s in D['segs'] if s.get('lnk2') is not None]
    cg = [bikes[s['id']]['g'] for s in cand]
    ctree = STRtree(cg)
    for i, s in enumerate(cand):
        tb = cg[i].buffer(40)
        s['adj'] = sorted(int(cand[k]['id']) for k in ctree.query(tb) if k != i and cg[k].intersects(tb))
    # where serious crashes happen: major streets vs their share of street miles
    if U6 is not None:
        major = unary_union([g for g in art + col])
        allst = unary_union([transform(tr, shape(f['geometry'])) for f in sf
                             if f['geometry'] and f['properties'].get('class') in ('1', '2', '3', '4')])
        mb = major.buffer(60)
        ksi = [c for c in D['crashes'] if c[3] in ('K', 'A')]
        inside = [pt(c[1], c[0]) for c in ksi]
        inside = [p_ for p_ in inside if U6.contains(p_)]
        on_major = sum(1 for p_ in inside if mb.contains(p_))
        D['meta']['ksi_major_pct'] = round(100 * on_major / len(inside)) if inside else None
        D['meta']['major_miles_pct'] = round(100 * major.intersection(U6).length / allst.intersection(U6).length)
    for o in D['bgs']:
        if o['id'] in eq.index:
            r = eq.loc[o['id']]
            o['zc'] = round(100 * r.zc / r.hh, 1) if r.hh else None
            o['pov'] = round(100 * r.pp / r.pu, 1) if r.pu else None


# --------------------------------------------------------------------------
# 7. IDOT road measurements
# --------------------------------------------------------------------------
def idot(D, bikes):
    import math
    R = json.load(open(RAW / 'idot_roads.geojson'))['features']

    def num(v):
        return None if v is None or v == '' else float(v)
    items = []
    for f in R:
        if not f['geometry']:
            continue
        p = f['properties']
        g = transform(tr, shape(f['geometry']))
        items.append((g, {k: p.get(k) for k in ('ROAD_NAME', 'FC', 'JUR_TYPE', 'LNS', 'SURF_WTH', 'PRK_LT', 'PRK_RT', 'SP_LIM',
                                                'AADT', 'AADT_YR', 'OP_1_2_WAY', 'MED_TYP')}))
    geoms = [g for g, _ in items]
    tree = STRtree(geoms)

    def angle(g):
        c = list(g.coords) if g.geom_type == 'LineString' else list(g.geoms[0].coords)
        (x1, y1), (x2, y2) = c[0], c[-1]
        return math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180

    def parallel(a, b):
        d = abs(angle(a) - angle(b))
        return min(d, 180 - d) < 25
    matched = 0
    for s in D['segs']:
        g = bikes[s['id']]['g']
        bf = g.buffer(40)
        hits = []
        for k in tree.query(bf):
            gi = geoms[k].intersection(bf)
            if gi.length < 60:
                continue
            try:
                if not parallel(geoms[k], g):
                    continue
            except Exception:
                pass
            hits.append((gi.length, items[k][1]))
        s['st'] = re.sub(r'^[NSEW] ', '', s['st'])
        if not hits:
            s['idot'] = None
            continue
        matched += 1
        tot = sum(h for h, _ in hits)

        def wmode(key):
            c = collections.Counter()
            for h, p in hits:
                v = num(p[key])
                if v is not None:
                    c[v] += h
            return c.most_common(1)[0][0] if c else None

        def wmean(key, valid=lambda v: v > 0):
            a = [(h, num(p[key])) for h, p in hits if num(p[key]) is not None and valid(num(p[key]))]
            return sum(h * v for h, v in a) / sum(h for h, _ in a) if a else None
        W = wmean('SURF_WTH', lambda v: v > 9)
        s['idot'] = {'w': round(W) if W else None, 'l': wmode('LNS'), 'pl': wmode('PRK_LT'), 'pr': wmode('PRK_RT'),
                     'aadt': round(wmean('AADT') / 100) * 100 if wmean('AADT') else None, 'yr': wmode('AADT_YR'),
                     'sp': wmode('SP_LIM') or None, 'jur': wmode('JUR_TYPE'), 'fc': wmode('FC'), 'ow': wmode('OP_1_2_WAY'),
                     'cov': round(tot / g.length, 2)}
    log('  IDOT matched', matched, 'of', len(D['segs']))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def month_label(iso):
    import datetime
    d = datetime.date.fromisoformat(iso[:10])
    return d.strftime('%b %Y')


def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    rawmeta = json.load(open(RAW / '_meta.json'))
    report = {}
    log('1. bike routes')
    wards = load_wards()
    U6 = unary_union([wards[w][1] for w in T])
    bikes = apply_manual(load_city_bikes(), report)
    street_feats = load_streets()
    uns = build_unserved(bikes, street_feats, U6)
    json.dump({'type': 'FeatureCollection', 'features': uns}, open(BUILD / 'unserved_segments.geojson', 'w'))
    bikes = bikes + roundtrip_lonlat(json.loads(json.dumps(uns)))
    log('   ', len(bikes), 'routes incl.', len(uns), 'major-street pieces with no facility')
    segs = clipped_segments(bikes, U6)
    trails, why = build_trails(RAW / 'trails.geojson', street_feats, U6)
    report['trails_dropped'] = why
    for tl in trails:
        gi = tl['g'].intersection(U6)
        if not gi.is_empty and gi.length >= 20:
            segs.append(dict(i=None, g=gi, t='Off-Street Trail', cf=False, st=tl['name'], f='', to='', mi=gi.length / 5280))
    log('   ', len(trails), 'off-street trails,', round(sum(tl['mi_in'] for tl in trails), 1), 'miles inside the wards; dropped:', why)
    log('2. layers + ward summary')
    L = load_layers(wards)
    summ = ward_summary(wards, L)
    log('3-4. segment measures and map layers')
    D = segments_and_layers(bikes, wards, U6, segs, L, summ)
    current_label = month_label(rawmeta['bike_routes_updated'])
    log('5. history + detail')
    history_and_detail(D, bikes, U6, L, current_label)
    log('6. stress + equity')
    stress_and_equity(D, bikes, segs, L, U6)
    log('7. IDOT')
    idot(D, bikes)
    log('8. headline numbers')
    defs = [('Protected lanes only', lambda s: s['t'] == 'Protected Bike Lane'),
            ('Protected + contraflow', lambda s: s['t'] == 'Protected Bike Lane' or s['cf']),
            ('Protected + contraflow + greenways + trails', lambda s: s['t'] in LOW_STRESS or s['cf'])]
    conn = []
    for name, sel in defs:
        C = components(segs, sel)
        Ls = [sum(x['mi'] for x in c) for c in C]
        conn.append([name, round(sum(Ls), 1), len(C), round(Ls[0], 1)])
    un = {str(w): 0 for w in T}
    for s in D['segs']:
        if s['t'] == 'N':
            un[str(s['w'])] = round(un[str(s['w'])] + s['mi'], 2)
    un['all'] = round(sum(s['mi'] for s in D['segs'] if s['t'] == 'N'), 1)
    keep = {k: D['meta'][k] for k in ('lscomp', 'ksi_major_pct', 'major_miles_pct') if k in D['meta']}
    D['meta'] = {'wards': T, 'crash_max': D['meta']['crash_max'], 'bike_rows': rawmeta['bike_routes_updated'][:10],
                 'city': city_comparison(wards, L), 'conn': conn, 'manual': MANUAL_SOURCE_DATE,
                 'idot': f"IDOT road inventory (ROAD NETWORK layer, {rawmeta['idot_service'].split('/')[-1]}), downloaded {rawmeta['fetched']['idot']}",
                 'unserved_mi': un,
                 'acs_year': rawmeta['acs_year'], 'ridership_year': rawmeta['ridership_year'], 'bike_label': current_label}
    D['meta'].update(keep)
    D['trails'] = [{'n': tl['name'], 'mi': round(tl['mi_in'], 2), 'c': coords(ll(tl['g'], 4))} for tl in trails]
    D['meta']['trails_mi'] = round(sum(tl['mi_in'] for tl in trails), 1)
    D['meta']['trails_source'] = rawmeta.get('trails_source', 'OpenStreetMap')
    json.dump({'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': {'name': tl['name'], 'mi_in_wards': round(tl['mi_in'], 2), 'osm_ids': tl['osm_ids']},
         'geometry': json.loads(json.dumps(__import__('shapely.geometry', fromlist=['mapping']).mapping(ll(tl['g']))))}
        for tl in trails]}, open(BUILD / 'trails.geojson', 'w'))
    s_ = json.dumps(D, separators=(',', ':'))
    (BUILD / 'bikenet_data.json').write_text(s_)
    json.dump(report, open(BUILD / 'build_report.json', 'w'), indent=1)
    log('wrote', len(s_), 'bytes')
    for k, v in D.items():
        log('  ', k, len(json.dumps(v, separators=(',', ':'))))


if __name__ == '__main__':
    sys.exit(main())
