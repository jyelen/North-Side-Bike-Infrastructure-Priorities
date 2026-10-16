"""Off-street bike trails (lakefront, river and channel trails, park paths
built for bikes) from OpenStreetMap.

OpenStreetMap also draws some protected lanes and sidewalk bike paths as
separate "cycleway" lines next to a street. Those are not off-street trails,
so any path that runs within 50 ft of a street centerline for most of its
length is dropped. What remains is grouped into connected trails, and very
short pieces (under 0.15 mile) are dropped.
"""
import collections
import json

from shapely.geometry import shape
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from load import tr

MIN_MI = 0.15
NEAR_STREET_FT = 50
NEAR_SHARE = 0.6


def build_trails(path, street_features, area):
    raw = json.load(open(path))['features']
    streets = [transform(tr, shape(f['geometry'])) for f in street_features
               if f.get('geometry') and f['properties'].get('class') in ('1', '2', '3', '4')]
    near = unary_union(streets).buffer(NEAR_STREET_FT)
    reasons = collections.Counter()
    kept = []
    for f in raw:
        p = f['properties']
        if (p.get('access') in ('private', 'no')) or p.get('bicycle') == 'no':
            reasons['no public bike access'] += 1
            continue
        g = transform(tr, shape(f['geometry']))
        if g.length == 0:
            continue
        share = g.intersection(near).length / g.length
        if share > NEAR_SHARE:
            reasons['runs beside a street'] += 1
            continue
        kept.append((g, p))
    # group touching paths into trails
    geoms = [g for g, _ in kept]
    tree = STRtree(geoms)
    par = list(range(len(kept)))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    for a, g in enumerate(geoms):
        for b in tree.query(g.buffer(25)):
            if b != a and g.distance(geoms[b]) <= 25:
                par[find(a)] = find(b)
    comps = collections.defaultdict(list)
    for a in range(len(kept)):
        comps[find(a)].append(a)
    out = []
    for ids in comps.values():
        g = unary_union([geoms[i] for i in ids])
        if g.length / 5280 < MIN_MI:
            reasons['short piece'] += len(ids)
            continue
        if not g.intersects(area.buffer(1000)):
            reasons['outside the wards'] += len(ids)
            continue
        names = collections.Counter()
        for i in ids:
            n = kept[i][1].get('name')
            if n:
                names[n] += geoms[i].length
        name = names.most_common(1)[0][0] if names else 'Off-street path'
        inside = g.intersection(area)
        out.append({'name': name, 'g': g, 'mi': g.length / 5280, 'mi_in': inside.length / 5280,
                    'osm_ids': [kept[i][1].get('osm_id') for i in ids]})
    out.sort(key=lambda t: -t['mi_in'])
    return out, dict(reasons)
