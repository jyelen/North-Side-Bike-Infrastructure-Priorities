"""Sanity checks on a freshly built data file.

Compares data/build/bikenet_data.json (new) with the version committed in
git (previous) and writes data/build/refresh_report.md. Exits with an error
if anything looks broken, so a bad refresh never reaches the live page.

"FAIL" lines block the update. "Check" lines are worth a look but do not.
"""
import collections
import datetime as dt
import json
import subprocess
import sys

from config import BUILD, RAW, ROOT

FAIL, WARN, INFO = [], [], []


def fail(m):
    FAIL.append(m)


def warn(m):
    WARN.append(m)


def previous():
    try:
        txt = subprocess.run(['git', 'show', 'HEAD:data/build/bikenet_data.json'], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return json.loads(txt)
    except Exception:
        return None


def ratio_check(name, new, old, lo, hi):
    if old in (None, 0):
        return
    r = new / old
    if r < lo or r > hi:
        fail(f'{name} changed from {old:,} to {new:,} ({r - 1:+.0%}); expected within {lo - 1:+.0%} to {hi - 1:+.0%}.')


def headline(D):
    segs = D['segs']
    miles = collections.defaultdict(float)
    for s in segs:
        t = 'CF' if s['cf'] and s['t'] != 'P' else s['t']
        miles[t] += s['mi']
    ksi = sum(1 for c in D['crashes'] if c[3] in ('K', 'A'))
    killed = sum(1 for c in D['crashes'] if c[3] == 'K')
    return {
        'Segments': len(segs),
        'Protected lane miles (segments)': round(miles['P'], 1),
        'Painted/buffered/shared miles': round(miles['L'] + miles['BF'] + miles['S'], 1),
        'Major-street miles with no facility': D['meta']['unserved_mi']['all'],
        'People injured walking/biking (map)': len(D['crashes']),
        'Killed or seriously injured (map)': ksi,
        'Killed (map)': killed,
        'Crash data through': D['meta']['crash_max'],
        'Bike route file dated': D['meta']['bike_rows'],
        'Licensed businesses': len(D['biz']),
        'Census block groups': len(D['bgs']),
        'L stations': len(D['stations']),
        'Census ACS year': D['meta'].get('acs_year'),
        'Ridership year': D['meta'].get('ridership_year'),
    }


def main():
    D = json.load(open(BUILD / 'bikenet_data.json'))
    P = previous()
    meta = json.load(open(RAW / '_meta.json'))
    rep = json.load(open(BUILD / 'build_report.json')) if (BUILD / 'build_report.json').exists() else {}
    today = dt.date.today()

    # ---- structure
    for k in ['wards', 'streets', 'segs', 'crashes', 'stations', 'bgs', 'zoning', 'biz', 'wardsum', 'meta']:
        if not D.get(k):
            fail(f'Section "{k}" is empty.')
    if len(D['wards']) != 6:
        fail(f'Expected 6 wards, found {len(D["wards"])}.')

    # ---- completeness of segment measures
    segs = D['segs']
    for f, limit in [('rail', 0), ('den', 0), ('stress', 0), ('bk', 0.02), ('zc', 0.02), ('pov', 0.02)]:
        miss = sum(1 for s in segs if s.get(f) is None) / max(len(segs), 1)
        if miss > limit:
            fail(f'{miss:.0%} of segments have no value for "{f}".')
    matched = sum(1 for s in segs if s.get('idot')) / max(len(segs), 1)
    if matched < 0.85:
        fail(f'Only {matched:.0%} of segments matched an IDOT road record (normally all of them).')
    if not any(s['t'] == 'P' for s in segs):
        fail('No protected lanes found; the bike route file is probably wrong.')

    # ---- freshness
    cm = dt.date.fromisoformat(D['meta']['crash_max'])
    if (today - cm).days > 45:
        warn(f'Newest crash record is {cm} ({(today - cm).days} days old).')
    acs_jam = (meta.get('counts', {}).get('acs') or {}).get('not_available_values', 0)
    if acs_jam:
        warn(f'{acs_jam} Census values were "not available" and counted as zero.')

    # ---- hand-entered news additions
    applied = rep.get('manual_applied', {})
    for k in rep.get('manual_expected', []):
        if not applied.get(k):
            warn(f'Hand-entered change "{k}" no longer matches the city file. CDOT may have updated that street; '
                 f'review data/manual/manual_segments.json.')
    from shapely.geometry import MultiLineString

    def line(s):
        return MultiLineString([[(x, y) for y, x in part] for part in s['c']])
    for s in [s for s in segs if s.get('src') == 'news']:
        g = line(s)
        for o in segs:
            if o is s or o.get('src') or o['t'] != s['t']:
                continue
            share = g.intersection(line(o).buffer(0.0003)).length / g.length   # ~100 ft
            if share > 0.5:
                warn(f'{s["st"]} ({s["f"]} to {s["to"]}): the city file now shows the same kind of facility along '
                     f'{share:.0%} of this hand-entered stretch. Remove it from data/manual/manual_segments.json.')
                break

    # ---- compare with the previous version
    rows = []
    if P:
        ratio_check('Segment count', len(segs), len(P['segs']), 0.85, 1.20)
        ratio_check('Injured people on the map', len(D['crashes']), len(P['crashes']), 0.95, 1.30)
        ratio_check('Licensed businesses', len(D['biz']), len(P['biz']), 0.75, 1.30)
        ratio_check('Census block groups', len(D['bgs']), len(P['bgs']), 0.95, 1.05)
        ratio_check('Street basemap pieces', len(D['streets']), len(P['streets']), 0.90, 1.10)
        ratio_check('Zoning classes', len(D['zoning']), len(P['zoning']), 0.80, 1.25)
        if D['meta']['crash_max'] < P['meta']['crash_max']:
            fail(f'Crash data went backwards: {P["meta"]["crash_max"]} -> {D["meta"]["crash_max"]}.')
        hn, ho = headline(D), headline(P)
        for k in hn:
            rows.append((k, ho.get(k), hn[k]))
        # segments that appeared / disappeared / changed type
        key = lambda s: (s['st'], s['f'], s['to'])
        old = {key(s): s for s in P['segs']}
        new = {key(s): s for s in segs}
        added = [k for k in new if k not in old]
        removed = [k for k in old if k not in new]
        changed = [(k, old[k]['t'], new[k]['t']) for k in new if k in old and old[k]['t'] != new[k]['t']]
        INFO.append(f'{len(added)} segments added, {len(removed)} removed, {len(changed)} changed type.')
        for k, a, b in changed[:25]:
            INFO.append(f'  {k[0]} ({k[1]} to {k[2]}): {a} -> {b}')
        for k in added[:25]:
            INFO.append(f'  new: {k[0]} ({k[1]} to {k[2]}), {new[k]["t"]}')
        for k in removed[:25]:
            INFO.append(f'  gone: {k[0]} ({k[1]} to {k[2]})')
    else:
        rows = [(k, None, v) for k, v in headline(D).items()]
        INFO.append('No previous version to compare against.')

    # ---- report
    out = ['# Data refresh report', '', f'Built {today}. Sources fetched: ' +
           ', '.join(f'{k} {v}' for k, v in sorted(meta.get('fetched', {}).items())), '']
    out += ['## Result', '', ('**Blocked.** Problems below must be fixed before this goes live.' if FAIL
                              else '**Passed** automatic checks.'), '']
    if FAIL:
        out += ['### Problems (block the update)', ''] + [f'- {m}' for m in FAIL] + ['']
    if WARN:
        out += ['### Worth a look (do not block)', ''] + [f'- {m}' for m in WARN] + ['']
    out += ['## Headline numbers', '', '| | Before | After |', '|---|---|---|']
    out += [f'| {k} | {"" if a is None else a} | {b} |' for k, a, b in rows]
    out += ['', '## Segment changes', '', '```'] + INFO + ['```', '',
            'Not refreshed automatically: hand-entered news changes (data/manual), fatality stories, cost assumptions, '
            'research sources and archived bike-route snapshots.']
    (BUILD / 'refresh_report.md').write_text('\n'.join(out) + '\n')
    print('\n'.join(out))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
