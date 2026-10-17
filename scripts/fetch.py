"""The Long Race 주간 데이터 수집기 (GitHub Actions에서 실행)

- World Bank API + Our World in Data(OWID) 차트 CSV에서 지표를 받아
- data/raw/ 에 원본, data/panel.csv 에 통합 표(국가코드·연도·지표·값), data/catalog.json 에 지표 정보(단위 꼬리표 포함)를 저장
- 표준 라이브러리만 사용 (설치할 패키지 없음)
- 한 지표가 실패해도 나머지는 계속 진행하고, 실패 목록은 catalog.json 에 남김

단위 꼬리표(unit)는 조합 계산 때 '같은 기준끼리만' 섞도록 막는 용도:
  usd_nominal(명목 달러) / usd_ppp(물가보정 달러) / years / pct / count / births_per_woman / per_1000 / index / other
"""
import csv, io, json, os, time, urllib.request, urllib.error
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(__file__), '..', 'data')
RAW = os.path.join(OUT, 'raw')
UA = {'User-Agent': 'longrace-data-fetcher/1.0 (github actions)'}

# ── World Bank 지표 (코드, 짧은 이름, 단위) ──
WB = [
    ('NY.GDP.PCAP.CD',    'gdp_per_capita_usd',       'usd_nominal'),
    ('NY.GDP.PCAP.PP.CD', 'gdp_per_capita_ppp',       'usd_ppp'),
    ('NY.GDP.MKTP.CD',    'gdp_usd',                  'usd_nominal'),
    ('SP.POP.TOTL',       'population',               'count'),
    ('SP.DYN.LE00.IN',    'life_expectancy',          'years'),
    ('SP.DYN.TFRT.IN',    'fertility_rate',           'births_per_woman'),
    ('SH.DYN.MORT',       'under5_mortality',         'per_1000'),
    ('SP.URB.TOTL.IN.ZS', 'urban_share',              'pct'),
    ('IT.NET.USER.ZS',    'internet_users_share',     'pct'),
    ('EG.ELC.ACCS.ZS',    'electricity_access',       'pct'),
    ('SL.UEM.TOTL.ZS',    'unemployment_rate',        'pct'),
    ('FP.CPI.TOTL.ZG',    'inflation',                'pct'),
    ('NE.EXP.GNFS.ZS',    'exports_share_gdp',        'pct'),
    ('SE.XPD.TOTL.GD.ZS', 'education_spending_gdp',   'pct'),
    ('SH.XPD.CHEX.GD.ZS', 'health_spending_gdp',      'pct'),
    ('MS.MIL.XPND.GD.ZS', 'military_spending_gdp',    'pct'),
    ('EN.GHG.CO2.PC.CE.AR5', 'co2_per_capita_t',      'other'),
    ('SI.POV.GINI',       'gini_index',               'index'),
    ('SP.POP.65UP.TO.ZS', 'age65_share',              'pct'),
    ('SE.TER.ENRR',       'tertiary_enrollment',      'pct'),
]

# ── OWID 차트 (주소 이름, 짧은 이름, 단위). 차트 이름이 바뀌면 실패 목록에 남음 ──
OWID = [
    ('life-expectancy',                       'owid_life_expectancy',     'years'),
    ('mean-years-of-schooling-long-run',      'mean_years_schooling',     'years'),
    ('gdp-per-capita-maddison-project-database', 'gdp_per_capita_maddison', 'usd_ppp'),
    ('human-development-index',               'hdi',                      'index'),
    ('happiness-cantril-ladder',              'life_satisfaction',        'index'),
    ('share-of-population-in-extreme-poverty','extreme_poverty_share',    'pct'),
    ('homicide-rate-unodc',                   'homicide_rate',            'per_100k'),
    ('children-per-woman-un',                 'owid_fertility',           'births_per_woman'),
    ('annual-working-hours-per-worker',       'working_hours',            'other'),
    ('daily-per-capita-caloric-supply',       'calories_per_day',         'other'),
]


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if i == tries - 1: raise
            time.sleep(3 * (i + 1))


def wb_countries():
    """실제 국가만(지역·소득그룹 합계 제외). {iso3: 이름}"""
    js = json.loads(get('https://api.worldbank.org/v2/country?format=json&per_page=400'))
    return {c['id']: c['name'] for c in js[1] if c.get('region', {}).get('id') not in (None, '', 'NA')}


def fetch_wb(code, countries):
    rows, page, pages = [], 1, 1
    while page <= pages:
        js = json.loads(get(f'https://api.worldbank.org/v2/country/all/indicator/{code}?format=json&per_page=20000&date=1950:2030&page={page}'))
        if not isinstance(js, list) or len(js) < 2 or js[1] is None:
            raise ValueError(f'빈 응답: {str(js)[:200]}')
        pages = js[0].get('pages', 1)
        for d in js[1]:
            iso = d.get('countryiso3code') or ''
            if iso in countries and d.get('value') is not None:
                rows.append((iso, countries[iso], int(d['date']), float(d['value'])))
        page += 1
    return rows


def fetch_owid(slug):
    raw = get(f'https://ourworldindata.org/grapher/{slug}.csv?v=1&csvType=full&useColumnShortNames=true')
    rd = csv.reader(io.StringIO(raw.decode('utf-8-sig')))
    head = next(rd)
    hl = [h.lower() for h in head]
    ie, ic, iy = hl.index('entity'), hl.index('code'), hl.index('year')
    vcols = [i for i in range(len(head)) if i not in (ie, ic, iy)]
    out = {}
    for r in rd:
        code = r[ic]
        if not code or code.startswith('OWID'): continue        # 지역 합계 제외
        for i in vcols:
            try: v = float(r[i])
            except (ValueError, IndexError): continue
            out.setdefault(head[i], []).append((code, r[ie], int(r[iy]), v))
    unit_note = ''
    try:
        meta = json.loads(get(f'https://ourworldindata.org/grapher/{slug}.metadata.json'))
        cols = meta.get('columns', {})
        if cols:
            c0 = next(iter(cols.values())); unit_note = c0.get('unit') or c0.get('shortUnit') or ''
    except Exception:
        pass
    return out, unit_note


def main():
    os.makedirs(RAW, exist_ok=True)
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')
    catalog, failed, panel = [], [], []

    try:
        countries = wb_countries()
    except Exception as e:
        countries = {}; failed.append({'source': 'worldbank', 'id': 'country-list', 'error': str(e)[:200]})

    for code, name, unit in WB:
        if not countries: break
        try:
            rows = fetch_wb(code, countries)
            if not rows: raise ValueError('데이터 0행')
            with open(os.path.join(RAW, f'wb_{name}.csv'), 'w', newline='', encoding='utf-8') as f:
                w = csv.writer(f); w.writerow(['iso3', 'country', 'year', name]); w.writerows(sorted(rows))
            panel += [(iso, cn, y, name, v) for iso, cn, y, v in rows]
            catalog.append({'name': name, 'source': 'World Bank', 'id': code, 'unit': unit, 'rows': len(rows),
                            'years': [min(r[2] for r in rows), max(r[2] for r in rows)],
                            'url': f'https://data.worldbank.org/indicator/{code}'})
            print('OK  wb  ', name, len(rows))
        except Exception as e:
            failed.append({'source': 'worldbank', 'id': code, 'error': str(e)[:200]}); print('FAIL wb', code, e)
        time.sleep(0.5)

    for slug, name, unit in OWID:
        try:
            cols, unit_note = fetch_owid(slug)
            if not cols: raise ValueError('데이터 0행')
            for j, (col, rows) in enumerate(cols.items()):
                nm = name if j == 0 else f'{name}__{col}'
                with open(os.path.join(RAW, f'owid_{nm}.csv'), 'w', newline='', encoding='utf-8') as f:
                    w = csv.writer(f); w.writerow(['iso3', 'country', 'year', nm]); w.writerows(sorted(rows))
                panel += [(iso, cn, y, nm, v) for iso, cn, y, v in rows]
                catalog.append({'name': nm, 'source': 'Our World in Data', 'id': slug, 'column': col, 'unit': unit,
                                'unit_note': unit_note, 'rows': len(rows),
                                'years': [min(r[2] for r in rows), max(r[2] for r in rows)],
                                'url': f'https://ourworldindata.org/grapher/{slug}'})
                print('OK  owid', nm, len(rows))
        except Exception as e:
            failed.append({'source': 'owid', 'id': slug, 'error': str(e)[:200]}); print('FAIL owid', slug, e)
        time.sleep(0.5)

    panel.sort()
    with open(os.path.join(OUT, 'panel.csv'), 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f); w.writerow(['iso3', 'country', 'year', 'indicator', 'value']); w.writerows(panel)
    with open(os.path.join(OUT, 'catalog.json'), 'w', encoding='utf-8') as f:
        json.dump({'fetched_at': now, 'indicators': catalog, 'failed': failed}, f, ensure_ascii=False, indent=1)
    print(f'\n완료: 지표 {len(catalog)}개, 실패 {len(failed)}개, 통합 표 {len(panel)}행')


if __name__ == '__main__':
    main()
