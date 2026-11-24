"""三菱UFJアセットマネジメントのサイトが公開している基準価額CSV（設定来）から最新の基準価額を取り、fund-nav.json に書く。
（同社の投信情報APIはクラウドからのアクセスを403で弾くため、サイトのCSVを使う）"""
import json, re, sys, time, urllib.request, datetime, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
funds = []
for line in (ROOT / 'fund-codes.txt').read_text(encoding='utf-8').splitlines():
    parts = line.split('#')[0].split()
    if len(parts) >= 2 and re.fullmatch(r'[0-9A-Z]{8}', parts[0]) and re.fullmatch(r'\d{6}', parts[1]):
        funds.append((parts[0], parts[1]))

out_path = ROOT / 'fund-nav.json'
old = {}
if out_path.exists():
    try: old = json.loads(out_path.read_text(encoding='utf-8')).get('navs', {})
    except Exception: old = {}

navs, errors = dict(old), []
for assoc, fcd in funds:
    url = f'https://www.am.mufg.jp/fund_file/setteirai/{fcd}.csv'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (asset-app fund-nav)'})
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode('cp932', errors='replace')
        rows = [l.split(',') for l in text.splitlines() if re.match(r'^\d{4}/\d{2}/\d{2},', l)]
        last, prev = rows[-1], rows[-2]
        nav, pnav = float(last[1]), float(prev[1])
        navs[assoc] = {'nav': nav, 'prev': pnav, 'base_date': last[0], 'fund_cd': fcd}
        print(f'OK {assoc} {nav} ({last[0]})')
    except Exception as e:
        errors.append(f'{assoc}: {e}')
        print(f'NG {assoc}: {e}', file=sys.stderr)
    time.sleep(2)

jst = datetime.timezone(datetime.timedelta(hours=9))
out_path.write_text(json.dumps({'updated': datetime.datetime.now(jst).strftime('%Y-%m-%d %H:%M'), 'navs': navs, 'errors': errors},
                               ensure_ascii=False, indent=1), encoding='utf-8')
if funds and len(errors) == len(funds):
    sys.exit('全ファンドの取得に失敗しました')
