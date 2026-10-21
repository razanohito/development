"""取引履歴の集計と、推定ルール (dipstack_bt.py) のパラメータ探索。

    python3 fetch_data.py   # data/XAUUSD_1m.csv.gz を用意
    python3 study.py        # results/ に results.md, grid.csv, equity.png を出力
"""
from __future__ import annotations

import itertools
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = ["IPAGothic", "DejaVu Sans"]
import numpy as np
import pandas as pd

import dipstack_bt as bt

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "results"
USDJPY = 158.0          # 取引履歴から逆算した 10/12〜16 のレート (157.7〜158.6)
SPREAD = 0.30           # USD
SWAP = 30.0             # USD / ロット / 日 (買い)。10/16 の未表示分 -14,504円 / 6本 から推定
LOTS = 0.50             # 取引履歴と同じ
PER_LEVEL = 3           # 1 段で何本買うか (履歴は 2〜6 本、多いのは 3 本)
LOOKBACK = 60           # 分
EMA = 50                # H1 EMA
SPLIT = pd.Timestamp("2026-07-06")

GRID = dict(
    dip=[5.0, 10.0, 20.0],
    step=[5.0, 9.0, 15.0],
    max_levels=[1, 2, 3, 5, 10],
    tp=[3.0, 5.0, 10.0, 20.0],
    sl=[0.0, 30.0, 60.0],
    trend=[0, 1],
)


def summarize_history() -> tuple[pd.DataFrame, str]:
    d = pd.read_csv(HERE / "trades.csv", parse_dates=["close_time"]).sort_values("close_time")
    g = d[d.symbol == "GOLD_USD"].copy()
    g["move"] = g.close_price - g.open_price
    g["jpy_per_usd"] = g.profit_jpy / (g.move * g.lots * 100)
    by_day = d.groupby(d.close_time.dt.date).profit_jpy.agg(["count", "sum"])
    shown = {"2026-10-13": 635915, "2026-10-14": 364616, "2026-10-15": 210653, "2026-10-16": 1171450}
    lines = ["| 日付 | 本数 | 一覧の合計 | 画面上部の損益 | 差 (一覧に無い分) |", "|---|---|---|---|---|"]
    for day, row in by_day.iterrows():
        s = shown.get(str(day))
        diff = f"{s - row['sum']:+,}" if s is not None else "—"
        lines.append(f"| {day} | {row['count']} | {row['sum']:,} | {s:,} | {diff} |" if s is not None
                     else f"| {day} | {row['count']} | {row['sum']:,} | (写っていない) | {diff} |")
    return g, "\n".join(lines)


def run_grid(m1: pd.DataFrame) -> pd.DataFrame:
    arrs = bt.prepare(m1)
    roll_high, trend = bt.features(m1, LOOKBACK, EMA)
    tmin = bt.minutes(m1)
    split_i = int(np.searchsorted(m1["time"].to_numpy(), np.datetime64(SPLIT)))
    periods = {"full": (0, len(m1)), "h1": (0, split_i), "h2": (split_i, len(m1))}
    rows = []
    keys = list(GRID)
    for vals in itertools.product(*GRID.values()):
        p = dict(zip(keys, vals))
        if p["max_levels"] == 1 and p["step"] != GRID["step"][0]:
            continue  # 1 段だけなら幅は無関係
        row = dict(p)
        for name, (s, e) in periods.items():
            pnl, dd, maxlots, _, _ = bt.run(*arrs, roll_high, trend, LOTS, PER_LEVEL, p["dip"], p["step"],
                                         p["max_levels"], p["tp"], p["sl"], p["trend"], SPREAD, SWAP, s, e, tmin)
            gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
            row[f"{name}_net_jpy"] = pnl.sum() * USDJPY
            row[f"{name}_n"] = len(pnl)
            row[f"{name}_win"] = (pnl > 0).mean() if len(pnl) else np.nan
            row[f"{name}_pf"] = gp / gl if gl > 0 else np.inf
            row[f"{name}_dd_jpy"] = dd * USDJPY
            row[f"{name}_worst_jpy"] = pnl.min() * USDJPY if len(pnl) else 0.0
            row[f"{name}_maxlots"] = maxlots
        rows.append(row)
    return pd.DataFrame(rows)


def equity(m1: pd.DataFrame, p: dict) -> pd.Series:
    arrs = bt.prepare(m1)
    roll_high, trend = bt.features(m1, LOOKBACK, EMA)
    pnl, _, _, eq, span = bt.run(*arrs, roll_high, trend, LOTS, PER_LEVEL, p["dip"], p["step"], p["max_levels"],
                                 p["tp"], p["sl"], p["trend"], SPREAD, SWAP, 0, len(m1), bt.minutes(m1))
    return pd.Series(eq * USDJPY, index=m1["time"]), pnl, span


def fmt_row(r: pd.Series) -> str:
    sl = "なし" if r.sl == 0 else f"${r.sl:.0f}"
    return (f"| ${r.dip:.0f} | ${r.step:.0f} | {int(r.max_levels)} | ${r.tp:.0f} | {sl} | {'有' if r.trend else '無'} "
            f"| {int(r.full_n)} | {r.full_win:.0%} | {r.full_pf:.2f} | {r.full_net_jpy/1e4:+,.0f}万 "
            f"| {r.full_dd_jpy/1e4:,.0f}万 | {r.full_worst_jpy/1e4:+,.0f}万 | {r.full_maxlots:.1f} "
            f"| {r.h1_net_jpy/1e4:+,.0f}万 | {r.h2_net_jpy/1e4:+,.0f}万 |")


HEAD = ("| 押し目 | 買い増し幅 | 最大段数 | 利確 | 損切り | H1トレンド | バスケット数 | 勝率 | PF | 純損益 | 最大DD "
        "| 最悪1回 | 最大建玉 | 前半 | 後半 |\n|" + "---|" * 15)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    g, day_table = summarize_history()
    m1 = bt.load_m1()
    res = run_grid(m1)
    res.to_csv(OUT / "grid.csv", index=False, float_format="%.4f")

    n = len(res)
    prof = res[res.full_net_jpy > 0]
    both = res[(res.h1_net_jpy > 0) & (res.h2_net_jpy > 0)]
    nosl = res[res.sl == 0]
    nosl_bust = (nosl.full_dd_jpy > 5e6).mean()
    # 履歴に近い設定: 押し目 $5, 幅 $9, 最大 3 段, 利確 $5, 損切りなし, フィルタなし
    near = res[(res.dip == 5) & (res.step == 9) & (res.max_levels == 3) & (res.tp == 5) & (res.sl == 0) & (res.trend == 0)].iloc[0]
    # 両半期黒字の中で DD が最小のもの
    best = both.sort_values("full_dd_jpy").iloc[0] if len(both) else res.sort_values("full_net_jpy").iloc[-1]
    top_net = res.sort_values("full_net_jpy", ascending=False).head(10)
    top_safe = both.assign(ratio=both.full_net_jpy / both.full_dd_jpy).sort_values("ratio", ascending=False).head(10)

    fig, ax = plt.subplots(figsize=(10, 4.5))
    near_eq, near_pnl, near_span = equity(m1, near)
    near_low_day = near_eq.idxmin()
    w = int(np.argmin(near_pnl))
    w_open, w_close = m1["time"].iloc[near_span[w, 0]], m1["time"].iloc[near_span[w, 1]]
    w_px = m1["open"].iloc[near_span[w, 0]]
    n_win = int((near_pnl > 0).sum())
    for p, lab in [(near, "履歴に近い設定"), (best, "前後半とも黒字・DD最小")]:
        e = equity(m1, p)[0]
        ax.plot(e.index, e.values / 1e4, lw=1, label=lab)
    ax2 = ax.twinx()
    ax2.plot(m1["time"], m1["close"], color="0.75", lw=0.6)
    ax2.set_ylabel("金価格 (USD)")
    ax.set_ylabel("含み損込み損益 (万円)")
    ax.axhline(0, color="0.4", lw=0.5)
    ax.legend(loc="lower left")
    ax.set_title("0.50ロット x 3本/段 の押し目まとめ買い (2026-03〜09, 1分足)")
    fig.tight_layout()
    fig.savefig(OUT / "equity.png", dpi=110)

    gm = g.move
    md = f"""# Chieko さんの取引履歴の検証

## 1. 取引履歴の読み取り (trades.csv)

* 金 (GOLD_USD) の買い 0.50 ロット x 33 本と GBPJPY 売り 3.00 ロット x 1 本。売りの金は 0 本。負けた取引は 1 本も写っていない。
* 時刻は**決済時刻**。同じ時刻帯の決済価格は数ドル以内に揃うが、建値は最大 $18 離れている (10/13 07:38 台: 4172 / 4181 / 4190)。
* 1 本あたりの値幅: 最小 ${gm.min():.2f}、中央値 ${gm.median():.2f}、最大 ${gm.max():.2f}。利確幅は一定ではない。
* 円換算レートは日ごとに 157.7〜158.6 で一定 (MT5 の通常計算と矛盾なし)。
* 同じ塊の決済が 4〜40 秒ずつずれている。EA の一括決済なら 1 秒以内に終わるので、**スマホで 1 本ずつ手動決済している可能性が高い**。

{day_table}

画面上部の損益が一覧の合計より少ない日 (10/13 -27,924円, 10/16 -14,504円) は、一覧に写っていない取引かスワップ・手数料がある。
10/16 は 6 本で -14,504円 = 1 本 約 -2,400円 (約 $15 / 0.5 ロット) で、1 晩分の買いスワップとほぼ同じ。

## 2. 推定した手法

* 金の押し目で **0.50 ロットを 2〜6 本 (多いのは 3 本) まとめて買う**。
* 下がったら **$8〜9 下で同じロットを追加** (10/13: 4190 → 4181 → 4172 の 3 段 x 2 本)。ロットは増やさない (マーチンではない)。
* 損切りは見えない。上がったところで手動決済 (10/12 は +$3 前後の小刻み、10/16 は +$22〜27 の大きな利確)。
* 1 段 3 本 = 1.5 ロット = 150 オンス。**$1 動くと 約 2.4 万円**、3 段 (4.5 ロット) なら約 7.1 万円。10/14 の 1 日の値幅は約 $97 で、3 段持っていれば最大 約 690 万円の含み損になり得た幅。

## 3. 推定ルールの機械化とバックテスト

GoldDipStackEA.mq5 / dipstack_bt.py: 直近 {LOOKBACK} 分高値から「押し目」ドル下げたら 1 段目 ({PER_LEVEL} 本 x {LOTS} ロット) を買い、
「買い増し幅」下げるごとに同ロットで追加、平均建値 +「利確」ドルで全決済、「損切り」ドルで全損切り (なしも検証)。
データ: XAUUSD 1 分足 2026-03-26〜2026-09-25 (getdata-finance 無料サンプル, MIT)。前半 = 〜{SPLIT:%Y-%m-%d} (下落局面), 後半 = それ以降。
スプレッド ${SPREAD:.2f}、買いスワップ ${SWAP:.0f}/ロット/日 (水曜 3 倍)、USDJPY {USDJPY:.0f} 固定。証拠金・ロスカットは計算に入れていない (最大DD の金額で判断する)。
10 月のデータは取得できなかったため、スクショの 5 日間そのものの再現はしていない。

* 探索した設定: {n} 通り。全期間で黒字 {len(prof)} 通り ({len(prof)/n:.0%})、前半・後半とも黒字 {len(both)} 通り ({len(both)/n:.0%})。
* 損切りなしの設定のうち最大DD が 500 万円を超えたもの: {nosl_bust:.0%}。

### 履歴に近い設定 (押し目 $5, 幅 $9, 最大 3 段, 利確 $5, 損切りなし)

{HEAD}
{fmt_row(near)}

この設定は {len(near_pnl)} 回中 {n_win} 回勝つが、{w_open:%Y-%m-%d} に約 ${w_px:,.0f} で買い始めたバスケットが 3 段 (4.5 ロット) のまま
{w_close:%Y-%m-%d} まで決済できず、この 1 回で {near_pnl[w]*USDJPY/1e4:,.0f} 万円 (スワップ込み)。含み損込み損益の底は {near_eq.min()/1e4:,.0f} 万円 ({near_low_day:%Y-%m-%d})。
ロットを 0.01 x 3 本にすれば金額はすべて 1/50 (最大DD 約 {near.full_dd_jpy/50/1e4:,.0f} 万円)。

### 純損益の上位 10

{HEAD}
""" + "\n".join(fmt_row(r) for _, r in top_net.iterrows()) + f"""

### 前半・後半とも黒字のうち 純損益/最大DD の上位 10

{HEAD}
""" + "\n".join(fmt_row(r) for _, r in top_safe.iterrows()) + """

最大DD = 確定損益の最高値から、足の安値での含み損込み損益までの最大の落ち込み。必要資金の目安は最大DD の 3 倍以上 (DD 30% 以内)。

![equity](equity.png)
"""
    (OUT / "results.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
