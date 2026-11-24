"""三菱UFJアセットマネジメントの公式API（無料・登録不要）から基準価額を取り、fund-nav.json に書く。
Apps Script からはこのAPIが弾かれる（CloudFrontの403）ため、GitHub Actions で取ってリポジトリに置く。"""
import json, re, sys, time, urllib.request, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
codes = []
for line in (ROOT / 'fund-codes.txt').read_text(encoding='utf-8').splitlines():
    c = line.split('#')[0].strip()
    if re.fullmatch(r'[0-9A-Z]{8}', c):
        codes.append(c)

out_path = ROOT / 'fund-nav.json'
old = {}
if out_path.exists():
    try: old = json.loads(out_path.read_text(encoding='utf-8')).get('navs', {})
    except Exception: old = {}

navs, errors = dict(old), []
for c in codes:
    url = f'https://developer.am.mufg.jp/fund_information_latest/association_fund_cd/{c}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (asset-app fund-nav)'})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode('utf-8'))
        d = (j.get('datasets') or [{}])[0]
        nav = float(str(d.get('nav')).replace(',', ''))
        cmp = float(str(d.get('cmp_prev_day') or '0').replace(',', ''))
        navs[c] = {'nav': nav, 'prev': nav - cmp, 'base_date': str(d.get('base_date', '')), 'name': d.get('fund_name', '')}
        print(f'OK {c} {nav} ({d.get("base_date")})')
    except Exception as e:
        errors.append(f'{c}: {e}')
        print(f'NG {c}: {e}', file=sys.stderr)
    time.sleep(1)

jst = datetime.timezone(datetime.timedelta(hours=9))
out_path.write_text(json.dumps({'updated': datetime.datetime.now(jst).strftime('%Y-%m-%d %H:%M'), 'navs': navs, 'errors': errors},
                               ensure_ascii=False, indent=1), encoding='utf-8')
if len(errors) == len(codes):
    sys.exit('全ファンドの取得に失敗しました')
