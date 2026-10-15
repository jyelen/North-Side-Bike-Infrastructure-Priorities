"""Find major streets with no bike facility.

City arterials (street centerline classes 2 and 3) inside the study wards,
minus any stretch that already has a bike route along the same street, cut
into pieces at crossing arterials at least a quarter mile apart.
"""
import collections

from shapely.geometry import Point, mapping, shape
from shapely.ops import linemerge, substring, transform, unary_union
from shapely.strtree import STRtree

from load import tr, inv


def street_name(p):
    return ' '.join(x for x in [p.get('pre_dir'), p.get('street_nam'), p.get('street_typ')] if x)


def build_unserved(bikes, street_features, UN):
    BY = collections.defaultdict(list)
    for b in bikes:
        BY[(b['p'].get('street') or '').upper().strip()].append(b['g'])

    def along(name):
        key = ' '.join(name.split()[1:]) if name.split() and name.split()[0] in ('N', 'S', 'E', 'W') else name
        gs = BY.get(key, [])
        return unary_union(gs).buffer(40) if gs else None

    art = collections.defaultdict(list)
    allst = []
    for f in street_features:
        if not f['geometry']:
            continue
        p = f['properties']
        g = transform(tr, shape(f['geometry']))
        allst.append((street_name(p), p.get('class'), g))
        if p.get('class') in ('2', '3'):
            art[street_name(p)].append(g)
    artU = {k: unary_union(v) for k, v in art.items()}
    cross_geoms = [g for _, _, g in allst]
    cross_tree = STRtree(cross_geoms)

    def nearest_cross(pt_, own):
        best = None
        for k in cross_tree.query(pt_.buffer(450)):
            n, c, g = allst[k]
            if n == own or not n:
                continue
            d = g.distance(pt_)
            if best is None or d < best[0] or (abs(d - best[0]) < 15 and c in ('2', '3') and best[2] not in ('2', '3')):
                best = (d, n, c)
        return best[1] if best else 'edge of the study area'

    out = []
    for name, G in artU.items():
        if name.endswith('LAKE SHORE DR') or 'LSD' in name:
            continue
        g = G.intersection(UN)
        BA = along(name)
        if BA is not None:
            g = g.difference(BA)
        if g.is_empty:
            continue
        m = linemerge(g) if g.geom_type == 'MultiLineString' else g
        parts = list(m.geoms) if hasattr(m, 'geoms') else [m]
        for line in parts:
            if line.geom_type != 'LineString' or line.length < 300:
                continue
            cuts = set()
            for other, OG in artU.items():
                if other == name or not OG.intersects(line.buffer(5)):
                    continue
                X = line.intersection(OG.buffer(5))
                for x in (X.geoms if hasattr(X, 'geoms') else [X]):
                    if not x.is_empty:
                        cuts.add(round(line.project(x.centroid)))
            cuts = sorted(c for c in cuts if 900 < c < line.length - 900)
            cc = []
            for c in cuts:
                if not cc or c - cc[-1] > 1250:
                    cc.append(c)
            pos = [0] + cc + [line.length]
            for a, b_ in zip(pos[:-1], pos[1:]):
                piece = substring(line, a, b_)
                if piece.length < 400:
                    continue
                s = Point(piece.coords[0])
                e = Point(piece.coords[-1])
                out.append({'type': 'Feature',
                            'properties': {'street': name, 'f_street': nearest_cross(s, name), 't_street': nearest_cross(e, name),
                                           'displayrou': 'No bike facility', 'oneway_dir': '-', 'br_oneway': '2W', 'br_ow_dir': '-',
                                           'contraflow': None, 'mi_ctrline': str(piece.length / 5280), 'src': None},
                            'geometry': mapping(transform(inv, piece))})
    return out
