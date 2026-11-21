"""全資産の定期点検（asset_check）

毎月 GitHub Actions で実行し、資産ごとの成績・リスク・分散効果・割高度を計算して
「その資産が役割を果たしているか（優位性が続いているか）」を目安として判定する。

データ源（すべて無料・APIキー不要）
- Yahoo Finance（yfinance）：投資信託・ETF・指数・暗号資産の月次（配当込みに調整した終値）
- FRED：米国CPI、ドル円、米国10年・3か月金利、日本10年金利・短期金利・CPI
- GitHub datasets：金の長期月次価格（1833年〜）

出力
- asset_check/results/latest.json   … 最新の結果（ダッシュボードと定期タスクが読む）
- asset_check/results/history/YYYY-MM.json … 毎月の保存（後から判定の変化を追える）
- asset_check/results/report.md      … 人が読む要約

注意：判定は過去データにもとづく目安。売買の指示ではない。
"""
from __future__ import annotations

import io
import json
import math
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

OUT_DIR = os.path.join(os.path.dirname(__file__), "results")
OFFLINE_DIR = os.environ.get("ASSET_CHECK_OFFLINE")  # テスト用：CSVを置いたフォルダ

# ---------------------------------------------------------------------------
# 資産の一覧
#   role：その資産に期待する役割（判定の基準が変わる）
#     growth（長期の値上がり） / stable（安定・分散） / hedge（危機・インフレへの備え）
#     income（利回り） / speculative（高リスク高リターン） / cash（現金）
#   src：yahoo / fred_bond（金利から計算） / fred_cash / gold_long
#   cur：その資産の値段の通貨（USD / JPY）
# 長い歴史がある投資信託（Vanguard など）を優先し、無いものはETFで代用する。
# ---------------------------------------------------------------------------
ASSETS = [
    # 株式
    dict(id="us_stock", name="米国株（S&P500）", cls="株式", role="growth", src="yahoo", ticker="VFINX", cur="USD"),
    dict(id="world_stock", name="全世界株（オルカン相当）", cls="株式", role="growth", src="yahoo", ticker="VT", cur="USD", alt=["ACWI"]),
    dict(id="dev_stock", name="先進国株（米国以外）", cls="株式", role="growth", src="yahoo", ticker="VTMGX", cur="USD", alt=["EFA"]),
    dict(id="em_stock", name="新興国株", cls="株式", role="growth", src="yahoo", ticker="VEIEX", cur="USD", alt=["EEM"]),
    dict(id="jp_stock", name="日本株（TOPIX連動ETF）", cls="株式", role="growth", src="yahoo", ticker="1306.T", cur="JPY"),
    dict(id="nikkei", name="日経平均（配当なし）", cls="株式", role="growth", src="yahoo", ticker="^N225", cur="JPY"),
    dict(id="us_small", name="米国小型株", cls="株式", role="growth", src="yahoo", ticker="NAESX", cur="USD", alt=["IWM"]),
    dict(id="us_value", name="米国バリュー株", cls="株式", role="growth", src="yahoo", ticker="VIVAX", cur="USD", alt=["VTV"]),
    # 債券・現金
    dict(id="us_tbill", name="米ドル現金（3か月国債）", cls="債券・現金", role="cash", src="fred_cash", series="TB3MS", cur="USD"),
    dict(id="us_10y", name="米国債10年（金利から計算）", cls="債券・現金", role="stable", src="fred_bond", series="GS10", cur="USD"),
    dict(id="us_long_tsy", name="米国長期国債", cls="債券・現金", role="stable", src="yahoo", ticker="VUSTX", cur="USD", alt=["TLT"]),
    dict(id="us_agg", name="米国総合債券", cls="債券・現金", role="stable", src="yahoo", ticker="VBMFX", cur="USD", alt=["AGG"]),
    dict(id="tips", name="米国物価連動国債", cls="債券・現金", role="hedge", src="yahoo", ticker="VIPSX", cur="USD", alt=["TIP"]),
    dict(id="us_hy", name="米国ハイイールド債", cls="債券・現金", role="income", src="yahoo", ticker="VWEHX", cur="USD", alt=["HYG"]),
    dict(id="em_bond", name="新興国債券（ドル建て）", cls="債券・現金", role="income", src="yahoo", ticker="EMB", cur="USD"),
    dict(id="jp_10y", name="日本国債10年（金利から計算）", cls="債券・現金", role="stable", src="fred_bond", series="IRLTLT01JPM156N", cur="JPY"),
    dict(id="jpy_cash", name="円の現金", cls="債券・現金", role="cash", src="fred_cash", series="IRSTCI01JPM156N", cur="JPY"),
    # 実物資産
    dict(id="gold", name="金", cls="実物資産", role="hedge", src="gold_long", cur="USD"),
    dict(id="silver", name="銀", cls="実物資産", role="hedge", src="yahoo", ticker="SI=F", cur="USD"),
    dict(id="commodity", name="コモディティ（総合）", cls="実物資産", role="hedge", src="yahoo", ticker="PCRIX", cur="USD", alt=["DBC"]),
    dict(id="oil", name="原油（WTI先物）", cls="実物資産", role="hedge", src="yahoo", ticker="CL=F", cur="USD"),
    dict(id="us_reit", name="米国REIT", cls="実物資産", role="income", src="yahoo", ticker="VGSIX", cur="USD", alt=["VNQ"]),
    dict(id="jp_reit", name="J-REIT（東証REIT指数ETF）", cls="実物資産", role="income", src="yahoo", ticker="1343.T", cur="JPY"),
    dict(id="infra", name="インフラ株", cls="実物資産", role="income", src="yahoo", ticker="IGF", cur="USD"),
    # 暗号資産
    dict(id="btc", name="ビットコイン", cls="暗号資産", role="speculative", src="yahoo", ticker="BTC-USD", cur="USD"),
    dict(id="eth", name="イーサリアム", cls="暗号資産", role="speculative", src="yahoo", ticker="ETH-USD", cur="USD"),
    # オルタナティブ（プライベート資産は上場の代わりで見る）
    dict(id="listed_pe", name="上場プライベート・エクイティ", cls="オルタナティブ", role="growth", src="yahoo", ticker="PSP", cur="USD",
         note="非上場ファンドそのものではない。非上場は評価が遅れて値動きが小さく見えるため、上場版の方が実態に近いとされる"),
    dict(id="private_credit", name="プライベート・クレジット（BDC）", cls="オルタナティブ", role="income", src="yahoo", ticker="BIZD", cur="USD",
         note="上場の事業開発会社（BDC）で代用"),
    dict(id="managed_futures", name="マネージド・フューチャーズ（トレンド追随）", cls="オルタナティブ", role="hedge", src="yahoo", ticker="AQMIX", cur="USD", alt=["DBMF"]),
    dict(id="hedge_multi", name="ヘッジファンド複製", cls="オルタナティブ", role="stable", src="yahoo", ticker="QAI", cur="USD"),
]

# 比較の基準（株式の代表）。分散効果は「米国株と比べて」計算する
BENCH = "us_stock"
WORLD = "world_stock"

FRED_SERIES = ["CPIAUCSL", "EXJPUS", "GS10", "TB3MS", "IRLTLT01JPM156N", "IRSTCI01JPM156N", "JPNCPIALLMINMEI"]


# ---------------------------------------------------------------------------
# データ取得
# ---------------------------------------------------------------------------
def _offline(name: str) -> pd.Series | None:
    if not OFFLINE_DIR:
        return None
    p = os.path.join(OFFLINE_DIR, f"{name}.csv")
    if not os.path.exists(p):
        return None
    s = pd.read_csv(p, index_col=0, parse_dates=True).iloc[:, 0]
    return to_month(s)


def to_month(s: pd.Series) -> pd.Series:
    s = s.dropna()
    s.index = pd.to_datetime(s.index).to_period("M").to_timestamp()
    return s.groupby(level=0).last()


def fred(series: str) -> pd.Series | None:
    off = _offline(series)
    if off is not None or OFFLINE_DIR:
        return off
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = ["date", "v"]
        df["v"] = pd.to_numeric(df.v, errors="coerce")
        return to_month(df.set_index(pd.to_datetime(df.date)).v)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] FRED {series}: {e}", file=sys.stderr)
        return None


def yahoo(tickers: list[str]) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    if OFFLINE_DIR:
        for t in tickers:
            s = _offline(t.replace("^", "_").replace("=", "_"))
            if s is not None:
                out[t] = s
        return out
    import yfinance as yf
    for t in tickers:
        try:
            df = yf.download(t, start="1960-01-01", interval="1mo", auto_adjust=True, progress=False, threads=False)
            if df is None or df.empty:
                print(f"[warn] Yahoo {t}: 空", file=sys.stderr)
                continue
            c = df["Close"]
            if isinstance(c, pd.DataFrame):
                c = c.iloc[:, 0]
            out[t] = to_month(c)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] Yahoo {t}: {e}", file=sys.stderr)
    return out


def gold_long() -> pd.Series | None:
    off = _offline("gold")
    if off is not None or OFFLINE_DIR:
        return off
    try:
        r = requests.get("https://raw.githubusercontent.com/datasets/gold-prices/main/data/monthly.csv", timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        return to_month(pd.Series(df.Price.values, index=pd.to_datetime(df.Date + "-01")))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] gold: {e}", file=sys.stderr)
        return None


def cape_now() -> float | None:
    """米国株のCAPE（シラーPER）の最新値。multpl.com の表から読む。取れなければ None"""
    if OFFLINE_DIR:
        v = os.environ.get("ASSET_CHECK_CAPE")
        return float(v) if v else None
    try:
        r = requests.get("https://www.multpl.com/shiller-pe/table/by-month", timeout=60,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        t = pd.read_html(io.StringIO(r.text))[0]
        v = str(t.iloc[0, 1]).replace("estimate", "").strip()
        return float(v)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] CAPE: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 価格系列づくり
# ---------------------------------------------------------------------------
def bond_index(yield_pct: pd.Series, maturity_years: int = 10) -> pd.Series:
    """毎月、満期10年の国債を額面で買い、翌月の金利で値洗いして売る（利息込みの値動き）"""
    y = (yield_pct / 100).dropna()
    n = maturity_years * 12 - 1
    rets = [np.nan]
    for k in range(1, len(y)):
        c, r = y.iloc[k - 1], y.iloc[k]
        i = r / 12
        if abs(i) < 1e-9:
            price = c / 12 * n + 1
        else:
            price = (c / 12) * (1 - (1 + i) ** -n) / i + (1 + i) ** -n
        rets.append(price - 1 + c / 12)
    return (1 + pd.Series(rets, index=y.index).fillna(0)).cumprod()


def cash_index(rate_pct: pd.Series) -> pd.Series:
    r = (rate_pct / 100).dropna() / 12
    return (1 + r.shift(1).fillna(0)).cumprod()


def build_series(yh: dict[str, pd.Series], fr: dict[str, pd.Series | None], gold: pd.Series | None):
    """資産ごとに『現地通貨建ての配当込み指数』を作る"""
    out = {}
    used = {}
    for a in ASSETS:
        s = None
        if a["src"] == "yahoo":
            for t in [a["ticker"]] + a.get("alt", []):
                if t in yh and len(yh[t]) >= 24:
                    s, used[a["id"]] = yh[t], t
                    break
        elif a["src"] == "fred_bond" and fr.get(a["series"]) is not None:
            s, used[a["id"]] = bond_index(fr[a["series"]]), f"FRED {a['series']}"
        elif a["src"] == "fred_cash" and fr.get(a["series"]) is not None:
            s, used[a["id"]] = cash_index(fr[a["series"]]), f"FRED {a['series']}"
        elif a["src"] == "gold_long" and gold is not None:
            s, used[a["id"]] = gold, "GitHub datasets/gold-prices"
        if s is not None:
            s = s.astype(float)
            if a["role"] != "speculative" and a["src"] == "yahoo":
                s = fix_splits(s, a["id"])
            out[a["id"]] = s
    return out, used


def fix_splits(s: pd.Series, name: str) -> pd.Series:
    """Yahooの分割調整が崩れたとき（1か月で価格が1/2以下や2.5倍以上）は、その月の変化を0とみなしてつなぐ"""
    r = s / s.shift(1)
    bad = r[(r < 0.4) | (r > 2.5)].dropna()
    if bad.empty:
        return s
    print(f"[info] {name}: 分割とみられる段差を補正 {[t.strftime('%Y-%m') for t in bad.index]}", file=sys.stderr)
    rr = r.copy()
    rr[bad.index] = 1.0
    rr.iloc[0] = 1.0
    return s.iloc[0] * rr.cumprod()


def to_currency(s: pd.Series, cur: str, target: str, usdjpy: pd.Series) -> pd.Series:
    if cur == target:
        return s
    fx = usdjpy.reindex(s.index).ffill()
    return (s * fx if target == "JPY" else s / fx).dropna()


# ---------------------------------------------------------------------------
# 指標
# ---------------------------------------------------------------------------
def cagr(s: pd.Series) -> float | None:
    s = s.dropna()
    if len(s) < 13:
        return None
    yrs = (len(s) - 1) / 12
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1)


def window(s: pd.Series, years: int) -> pd.Series | None:
    s = s.dropna()
    if len(s) < years * 12 + 1:
        return None
    return s.iloc[-(years * 12 + 1):]


def max_dd(s: pd.Series) -> float:
    return float((s / s.cummax() - 1).min())


def longest_under(s: pd.Series) -> tuple[float, str, str]:
    peak = s.cummax()
    best = (0.0, "", "")
    start = None
    prev = None
    for t, v in s.items():
        under = v < peak[t] * 0.9999
        if under and start is None:
            start = prev if prev is not None else t
        if not under and start is not None:
            L = (t - start).days / 365.25
            if L > best[0]:
                best = (L, start.strftime("%Y-%m"), t.strftime("%Y-%m"))
            start = None
        prev = t
    if start is not None:
        L = (s.index[-1] - start).days / 365.25
        if L > best[0]:
            best = (L, start.strftime("%Y-%m"), "未回復")
    return round(best[0], 1), best[1], best[2]


def rnd(x, n=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), n)


def metrics(a, s_loc, s_usd, s_jpy, cpi_us, cpi_jp, bench_usd):
    r = s_usd.pct_change().dropna()          # 相関はドル建てでそろえる
    rl = s_loc.pct_change().dropna()         # ぶれ・下落はその資産の通貨で測る
    real_usd = (s_usd / cpi_us.reindex(s_usd.index).ffill()).dropna() if cpi_us is not None else None
    real_jpy = (s_jpy / cpi_jp.reindex(s_jpy.index).ffill()).dropna() if cpi_jp is not None else None
    m = dict(
        id=a["id"], name=a["name"], cls=a["cls"], role=a["role"],
        start=s_usd.index[0].strftime("%Y-%m"), end=s_usd.index[-1].strftime("%Y-%m"),
        years=rnd((len(s_usd) - 1) / 12, 1),
        cagr_usd=rnd(cagr(s_usd) * 100 if cagr(s_usd) is not None else None),
        cagr_real_usd=rnd(cagr(real_usd) * 100 if real_usd is not None and cagr(real_usd) is not None else None),
        cagr_jpy=rnd(cagr(s_jpy) * 100 if cagr(s_jpy) is not None else None),
        cagr_real_jpy=rnd(cagr(real_jpy) * 100 if real_jpy is not None and cagr(real_jpy) is not None else None),
        vol=rnd(rl.std() * math.sqrt(12) * 100, 1),
        max_dd=rnd(max_dd(s_loc) * 100, 1),
        max_dd_jpy=rnd(max_dd(s_jpy) * 100, 1),
        worst_12m=rnd((s_loc / s_loc.shift(12) - 1).min() * 100, 1),
        cur=a["cur"],
    )
    m["cagr_real_local"] = m["cagr_real_jpy"] if a["cur"] == "JPY" else m["cagr_real_usd"]
    real_loc = real_jpy if a["cur"] == "JPY" else real_usd
    u = longest_under(real_loc if real_loc is not None and len(real_loc) > 24 else s_loc)
    m.update(longest_under_years=u[0], under_from=u[1], under_to=u[2])
    # 10年保有で実質マイナスだった割合
    if real_loc is not None and len(real_loc) > 12 * 12:
        r10 = (real_loc.shift(-120) / real_loc - 1).dropna()
        m["neg10_share"] = rnd((r10 < 0).mean() * 100, 1)
    else:
        m["neg10_share"] = None
    # 期間別（円建て）
    for yrs in (1, 3, 5, 10, 20):
        w = window(s_jpy, yrs)
        m[f"jpy_{yrs}y"] = rnd(cagr(w) * 100) if w is not None else None
    # 分散効果：米国株との相関（全期間・直近3年）と、株が大きく下げた月の平均
    if bench_usd is not None and a["id"] != BENCH:
        br = bench_usd.pct_change().dropna()
        j = pd.concat([r, br], axis=1, join="inner").dropna()
        j.columns = ["a", "b"]
        m["corr_us"] = rnd(j.a.corr(j.b)) if len(j) > 24 else None
        j36 = j.iloc[-36:]
        m["corr_us_3y"] = rnd(j36.a.corr(j36.b)) if len(j36) >= 30 else None
        crash = j[j.b < -0.05]
        m["crash_months"] = int(len(crash))
        m["crash_avg"] = rnd(crash.a.mean() * 100, 1) if len(crash) >= 5 else None
    else:
        m.update(corr_us=1.0 if a["id"] == BENCH else None, corr_us_3y=1.0 if a["id"] == BENCH else None,
                 crash_months=None, crash_avg=None)
    if bench_usd is not None and a["id"] == BENCH:
        br = bench_usd.pct_change().dropna()
        crash = br[br < -0.05]
        m["crash_months"] = int(len(crash))
        m["crash_avg"] = rnd(crash.mean() * 100, 1)
    # トレンド：12か月騰落と10か月平均との位置
    m["chg_12m"] = rnd((s_usd.iloc[-1] / s_usd.iloc[-13] - 1) * 100, 1) if len(s_usd) > 13 else None
    m["above_10m_avg"] = bool(s_usd.iloc[-1] > s_usd.iloc[-10:].mean()) if len(s_usd) > 10 else None
    m["source"] = None
    m["note"] = a.get("note")
    return m


# ---------------------------------------------------------------------------
# 判定（役割ごと）
#   status：良好 / 要注意 / 見直し
#   判定は「その資産が持つ役割を、今も果たしているか」の目安
# ---------------------------------------------------------------------------
def judge(m: dict, sig: dict) -> tuple[str, list[str]]:
    role = m["role"]
    reasons: list[str] = []
    level = 0  # 0 良好, 1 要注意, 2 見直し

    def bump(lv, msg):
        nonlocal level
        level = max(level, lv)
        reasons.append(msg)

    if role == "growth":
        # 長期で物価に勝っているか
        if m["cagr_real_local"] is not None and m["years"] and m["years"] >= 15:
            if m["cagr_real_local"] < 0:
                bump(2, f"全期間の実質年率が{m['cagr_real_usd']}%でマイナス")
            elif m["cagr_real_local"] < 3:
                bump(1, f"全期間の実質年率が{m['cagr_real_usd']}%と低い")
        if m.get("jpy_10y") is not None and m["jpy_10y"] < 0:
            bump(1, f"直近10年の円建て年率が{m['jpy_10y']}%")
        if m["id"] == BENCH and sig.get("ecy") is not None:
            e = sig["ecy"]
            if e < 0:
                bump(2, f"株の益回り（1/CAPE）が債券の実質利回りを下回る（差{e:.1f}%）")
            elif e < 1.5:
                bump(1, f"株と債券の期待リターンの差が小さい（差{e:.1f}%）")
        if m["id"] == BENCH and sig.get("cape") and sig["cape"] > 30:
            bump(1, f"CAPEが{sig['cape']:.1f}倍で割高（今後10年の期待は低め）")
    elif role == "stable":
        c3 = m.get("corr_us_3y")
        if c3 is not None:
            if c3 > 0.6:
                bump(2, f"直近3年の株との相関が{c3}で、分散の役目を果たしていない")
            elif c3 > 0.3:
                bump(1, f"直近3年の株との相関が{c3}とやや高い")
        if m.get("crash_avg") is not None and m["crash_avg"] < -2:
            bump(1, f"株の急落月に平均{m['crash_avg']}%下げる")
        if m["id"] == "us_10y" and sig.get("us_real_yield") is not None and sig["us_real_yield"] < 0:
            bump(1, f"米国債の実質利回りが{sig['us_real_yield']:.1f}%とマイナス")
    elif role == "hedge":
        if m.get("crash_avg") is not None and m["crash_avg"] < -3:
            bump(1, f"株の急落月に平均{m['crash_avg']}%下げ、守りになっていない")
        c3 = m.get("corr_us_3y")
        if c3 is not None and c3 > 0.6:
            bump(1, f"直近3年の株との相関が{c3}と高い")
        if m["id"] == "gold" and sig.get("gold_real_vs_median") and sig["gold_real_vs_median"] > 2.5:
            bump(1, f"金の実質価格が1971年以降の中央値の{sig['gold_real_vs_median']:.1f}倍で歴史的に割高")
        if m["cagr_real_local"] is not None and m["years"] and m["years"] >= 15 and m["cagr_real_local"] < -2:
            bump(1, f"全期間の実質年率が{m['cagr_real_usd']}%")
    elif role == "income":
        if m["cagr_real_local"] is not None and m["years"] and m["years"] >= 10 and m["cagr_real_local"] < 0:
            bump(1, f"全期間の実質年率が{m['cagr_real_usd']}%でマイナス")
        if m.get("corr_us_3y") is not None and m["corr_us_3y"] > 0.75:
            bump(1, f"直近3年は株とほぼ同じ動き（相関{m['corr_us_3y']}）")
    elif role == "speculative":
        if m["max_dd"] is not None and m["max_dd"] < -70:
            bump(1, f"最大下落{m['max_dd']}%。資産の一部にとどめる前提")
        if m.get("corr_us_3y") is not None and m["corr_us_3y"] > 0.5:
            bump(1, f"直近3年の株との相関が{m['corr_us_3y']}に上がり、分散効果は小さい")
    elif role == "cash":
        v = m["cagr_real_local"]
        if v is not None and v < 0:
            bump(1, f"実質年率{v}%（物価に負けている）")
    if not reasons:
        reasons.append("役割どおりの成績")
    return ["良好", "要注意", "見直し"][level], reasons


# ---------------------------------------------------------------------------
# 3つの「優位性」の判定（決めた基準で機械的に出す。1月に Claude が SPIVA などで確かめる）
#   続いている / 薄れている / 失われた
# ---------------------------------------------------------------------------
def window_cagr(s: pd.Series | None, years: int) -> float | None:
    if s is None:
        return None
    w = window(s, years)
    c = cagr(w) if w is not None else None
    return None if c is None else c * 100


def advantages(series_usd: dict[str, pd.Series], rows: list[dict], sig: dict, cpi_us) -> list[dict]:
    out = []
    by = {r["id"]: r for r in rows}

    # ① 米国株は世界の株より強いか：直近5年・10年の年率（ドル建て）で米国株と全世界株を比べる
    us, wd = series_usd.get("us_stock"), series_usd.get("world_stock")
    u5, w5, u10, w10 = window_cagr(us, 5), window_cagr(wd, 5), window_cagr(us, 10), window_cagr(wd, 10)
    if None not in (u5, w5, u10, w10):
        wins = int(u5 > w5) + int(u10 > w10)
        st = {2: "続いている", 1: "薄れている", 0: "失われた"}[wins]
        out.append(dict(key="us_vs_world", title="米国株は世界の株より強いか", status=st,
                        basis=f"直近5年 米国{u5:.1f}%・世界{w5:.1f}%／直近10年 米国{u10:.1f}%・世界{w10:.1f}%（ドル建て年率）",
                        rule="5年・10年とも米国が上→続いている、片方→薄れている、両方とも世界が上→失われた",
                        action="失われたら：新しい積立をS&P500からオルカンへ寄せる（すでに寄せていれば何もしない）"))
    # ② 株は債券より期待できるか：株の益回り（1/CAPE）と米国債の実質利回りの差
    e = sig.get("ecy")
    if e is not None:
        st = "続いている" if e >= 2 else ("薄れている" if e >= 0 else "失われた")
        out.append(dict(key="stock_vs_bond", title="株は債券より期待できるか", status=st,
                        basis=f"株の益回り（1/CAPE={sig.get('cape')}）− 米国債の実質利回り（{sig.get('us_real_yield')}%）＝{e:.1f}%",
                        rule="差が2%以上→続いている、0〜2%→薄れている、マイナス→失われた",
                        action="薄れている・失われたら：株を売るのではなく、今後10年の期待リターンを低めに置き、取り崩し率を3〜3.5%にする。失われた状態が2年続いたら、債券・現金の比率を少し上げる"))
    # ③ 債券・金は株の下げを和らげているか（分散の効き）
    b, g = by.get("us_10y"), by.get("gold")
    if b and b.get("corr_us_3y") is not None:
        c = b["corr_us_3y"]
        gc = g.get("crash_avg") if g else None
        st = "続いている" if c < 0.3 else ("薄れている" if c <= 0.6 else "失われた")
        out.append(dict(key="diversification", title="債券・金は株の下げを和らげているか", status=st,
                        basis=f"米国債10年と米国株の直近3年の相関 {c}／株の急落月の金の平均 {gc}%",
                        rule="相関0.3未満→続いている、0.3〜0.6→薄れている、0.6超→失われた",
                        action="失われたら：守りの役目を、現金・短期債・物価連動債・マネージド・フューチャーズに分ける"))
    # ④ インデックスはアクティブ運用に勝っているか（データが公開の表に無いので、毎年1月にClaudeが SPIVA で確かめる）
    out.append(dict(key="index_vs_active", title="インデックスはアクティブ運用に勝っているか", status="1月に確認",
                    basis="SPIVA（S&P社の調査）で、15年間にインデックスに負けたアクティブファンドの割合を見る",
                    rule="負けた割合が70%以上→続いている、50〜70%→薄れている、50%未満→失われた",
                    action="失われたら：低コストのアクティブファンドも候補に入れる"))
    return out


# ---------------------------------------------------------------------------
def main():
    os.makedirs(os.path.join(OUT_DIR, "history"), exist_ok=True)
    fr = {s: fred(s) for s in FRED_SERIES}
    tickers = sorted({t for a in ASSETS if a["src"] == "yahoo" for t in [a["ticker"]] + a.get("alt", [])})
    yh = yahoo(tickers)
    g = gold_long()
    series, used = build_series(yh, fr, g)

    usdjpy = fr.get("EXJPUS")
    if usdjpy is None:
        raise SystemExit("ドル円（FRED EXJPUS）が取れないため中止")
    # EXJPUSは月平均。Yahooの最新月がFREDより先にあるときは JPY=X で補う
    if "JPY=X" not in yh and not OFFLINE_DIR:
        yh.update(yahoo(["JPY=X"]))
    if "JPY=X" in yh:
        usdjpy = usdjpy.combine_first(yh["JPY=X"])
    cpi_us = fr.get("CPIAUCSL")
    cpi_jp = fr.get("JPNCPIALLMINMEI")

    # 割高度などのシグナル
    sig = {}
    cape = cape_now()
    sig["cape"] = cape
    if fr.get("GS10") is not None and cpi_us is not None:
        infl10 = ((cpi_us / cpi_us.shift(120)) ** 0.1 - 1) * 100
        ry = fr["GS10"].iloc[-1] - infl10.dropna().iloc[-1]
        sig["us_real_yield"] = round(float(ry), 2)
        if cape:
            sig["ecy"] = round(100 / cape - float(ry), 2)
    if fr.get("IRLTLT01JPM156N") is not None:
        sig["jp_10y_yield"] = round(float(fr["IRLTLT01JPM156N"].dropna().iloc[-1]), 2)
    if g is not None and cpi_us is not None:
        gr = (g / cpi_us.reindex(g.index).ffill()).dropna()
        med = gr[gr.index >= "1971-08-01"].median()
        # 金の最新月にCPIが無いときは、最後のCPIで割る（数か月の差は小さい）
        last_real = g.iloc[-1] / cpi_us.dropna().iloc[-1]
        sig["gold_real_vs_median"] = round(float(last_real / med), 2)
    sig["usdjpy"] = round(float(usdjpy.dropna().iloc[-1]), 1)

    bench_usd = None
    rows = []
    series_usd = {}
    # 先に米国株（基準）
    order = sorted(ASSETS, key=lambda a: a["id"] != BENCH)
    for a in order:
        s = series.get(a["id"])
        if s is None:
            rows.append(dict(id=a["id"], name=a["name"], cls=a["cls"], role=a["role"], status="データなし",
                             reasons=["データを取得できなかった"], source=None))
            continue
        s_usd = to_currency(s, a["cur"], "USD", usdjpy)
        s_jpy = to_currency(s, a["cur"], "JPY", usdjpy)
        series_usd[a["id"]] = s_usd
        if a["id"] == BENCH:
            bench_usd = s_usd
        m = metrics(a, s, s_usd, s_jpy, cpi_us, cpi_jp, bench_usd)
        m["source"] = used.get(a["id"])
        st, rs = judge(m, sig)
        m["status"], m["reasons"] = st, rs
        rows.append(m)
    # 元の並び順に戻す
    pos = {a["id"]: i for i, a in enumerate(ASSETS)}
    rows.sort(key=lambda r: pos[r["id"]])

    now = datetime.now(timezone.utc)
    data_end = max((r["end"] for r in rows if r.get("end")), default=None)
    result = dict(
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        data_end=data_end,
        signals=sig,
        advantages=advantages(series_usd, rows, sig, cpi_us),
        assets=rows,
        notes=[
            "配当・利息込み（Yahooの調整後終値）。日経平均だけは配当なし。",
            "実質はインフレ控除後。ドル建ては米国CPI、円建ては日本CPI（取れた月まで）。判定の実質年率は、その資産の通貨で測る。",
            "ぶれ・最大下落はその資産の通貨で測る（円建ての最大下落は max_dd_jpy）。株との相関と急落月の平均は、ドル建てでそろえて測る。",
            "米国債10年・日本国債10年・現金は金利から計算した理論上の値。",
            "プライベート資産は上場の代わりで見ている。非上場ファンドの実際の成績とは異なる。",
            "判定は過去データにもとづく目安で、売買の指示ではない。",
        ],
    )
    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    month = (data_end or now.strftime("%Y-%m"))
    with open(os.path.join(OUT_DIR, "history", f"{month}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    write_report(result)
    write_dashboard(result)
    print(f"OK: {len([r for r in rows if r.get('status') != 'データなし'])}/{len(rows)} 資産, データ末 {data_end}")


def write_dashboard(res: dict):
    """ダッシュボード（Claudeの Data 型アーティファクト）用：{rows, meta} 形式で出す"""
    code = open(__file__, encoding="utf-8").read()
    meta = [{"label": "asset_check/run.py", "value": code, "type": "code", "lang": "python"},
            {"label": "Input", "value": "Yahoo Finance（yfinance・調整後終値）、FRED、GitHub datasets/gold-prices、multpl.com（CAPE）"},
            {"label": "生成日時", "value": res["generated_at"]}]
    keep = ["id", "name", "cls", "role", "status", "start", "years", "cagr_real_local", "cagr_jpy", "jpy_10y", "jpy_5y",
            "vol", "max_dd", "max_dd_jpy", "corr_us_3y", "crash_avg", "longest_under_years", "neg10_share", "source", "note"]
    rows = []
    for r in res["assets"]:
        row = {k: r.get(k) for k in keep}
        row["reasons"] = "／".join(r.get("reasons", []))
        rows.append(row)
    with open(os.path.join(OUT_DIR, "dashboard_assets.json"), "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "meta": meta}, f, ensure_ascii=False)
    sig = dict(res["signals"])
    sig["data_end"] = res["data_end"]
    sig["generated_at"] = res["generated_at"]
    with open(os.path.join(OUT_DIR, "dashboard_signals.json"), "w", encoding="utf-8") as f:
        json.dump({"rows": [sig], "meta": meta}, f, ensure_ascii=False)


def write_report(res: dict):
    sig = res["signals"]
    L = [f"# 全資産の定期点検（データ末 {res['data_end']}）", "",
         f"- 米国株CAPE: {sig.get('cape')}　株と債券の期待リターン差（1/CAPE − 実質金利）: {sig.get('ecy')}%",
         f"- 米国10年の実質利回り: {sig.get('us_real_yield')}%　日本10年金利: {sig.get('jp_10y_yield')}%",
         f"- 金の実質価格（1971年以降の中央値比）: {sig.get('gold_real_vs_median')}倍　ドル円: {sig.get('usdjpy')}", "",
         "## 優位性の判定", "", *[f"- **{a['title']}**：{a['status']}（{a['basis']}）" for a in res.get("advantages", [])], "",
         "## 資産ごとの判定", "",
         "| 資産 | 判定 | 実質年率（各資産の通貨） | 円建て10年 | 最大下落 | 株との相関(3年) | 株急落月の平均 | 理由 |",
         "|---|---|---|---|---|---|---|---|"]
    for r in res["assets"]:
        L.append("| {name} | {status} | {a} | {b} | {c} | {d} | {e} | {f} |".format(
            name=r["name"], status=r.get("status"),
            a=r.get("cagr_real_local"), b=r.get("jpy_10y"), c=r.get("max_dd"), d=r.get("corr_us_3y"),
            e=r.get("crash_avg"), f="／".join(r.get("reasons", []))))
    L += ["", *[f"- {n}" for n in res["notes"]]]
    with open(os.path.join(OUT_DIR, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
