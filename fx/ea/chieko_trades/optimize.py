"""GoldDipStackEA の改良版パラメータ探索 (損切り必須・H4 SMA100・時間切れ・クールダウン)。

前回の探索 (study.py) と過去の EA 検証から分かった傾向を入れて、もう一度探索する。

* 損切りなしは 1 回の塩漬けで破綻する (study.py: 損切りなしは 100% が DD 500 万円超)
  → 損切りを必須にし、比較用に「なし」も残す。
* 金ナンピンEA の探索では H4 SMA100 フィルタが最も安定 → H1 EMA50 と並べて試す。
* 1 回の塩漬けが損益を決める → 時間切れ決済 (MaxHoldHours) と損切り後のクールダウンを追加。
* 前半だけで選ぶと後半で崩れる (金ナンピンEA) → 前半・後半・8 週間 x 15 期間・近傍・コスト悪化の全部で選ぶ。

    python3 fetch_data.py
    python3 optimize.py      # results/optimize.md, opt_candidates.csv, equity_opt.png
"""
from __future__ import annotations

import itertools
import multiprocessing as mp
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
USDJPY = 158.0
SPREAD, SWAP = 0.30, 30.0          # 通常コスト
SPREAD_BAD, SWAP_BAD = 0.50, 50.0  # コスト悪化
LOTS, PER_LEVEL = 0.01, 3          # EA の初期値と同じ (0.50 ロットなら金額はすべて 50 倍)
EMA = 50
SPLIT = pd.Timestamp("2026-07-06")
N_WIN, WIN_WEEKS = 15, 8           # 8 週間の期間を 1 週ずつずらして 15 本
MIN_BASKETS = 100                  # 5 か月で 100 回 (週 5 回) 未満は勝ち負けが偶然に左右されやすい
TRADE_START = pd.Timestamp("2026-04-20")  # H4 SMA100 のウォームアップ後 (金ナンピンEA と同じ)

GRID = dict(
    lookback=[60, 240, 480],            # 押し目判定の直近高値 (分)
    dip=[10.0, 20.0, 30.0, 40.0, 60.0, 80.0],
    step=[9.0, 15.0, 25.0, 40.0, 60.0],
    max_levels=[1, 2, 3, 5],
    tp=[3.0, 5.0, 10.0, 20.0, 30.0],
    sl=[0.0, 20.0, 40.0, 60.0, 100.0, 150.0],
    trend=[0, 1, 2],                 # 0 なし / 1 H1 EMA50 / 2 H4 SMA100
    hold_h=[0, 24, 72],              # 時間切れ (0 = なし)
    cool_h=[0, 24],                  # 損切り・時間切れの後の休み
)
TREND_NAME = {0: "なし", 1: "H1 EMA50", 2: "H4 SMA100"}

_S: dict = {}


def _init() -> None:
    m1 = bt.load_m1()
    m1 = m1.reset_index(drop=True)
    roll_high = {lb: bt.features(m1, lb, EMA)[0] for lb in GRID["lookback"]}
    tr1 = bt.trend_series(m1, "1h", EMA, "ema")
    tr2 = bt.trend_series(m1, "4h", 100, "sma")
    t = m1["time"].to_numpy()
    s0 = int(np.searchsorted(t, np.datetime64(TRADE_START)))
    split = int(np.searchsorted(t, np.datetime64(SPLIT)))
    wins = []
    for k in range(N_WIN):
        a = TRADE_START + pd.Timedelta(weeks=k)
        b = a + pd.Timedelta(weeks=WIN_WEEKS)
        wins.append((int(np.searchsorted(t, np.datetime64(a))), int(np.searchsorted(t, np.datetime64(b)))))
    _S.update(m1=m1, arrs=bt.prepare(m1), roll_high=roll_high, trends={0: tr1, 1: tr1, 2: tr2},
              tmin=bt.minutes(m1), periods={"full": (s0, len(m1)), "h1": (s0, split), "h2": (split, len(m1))},
              wins=wins)


def run_one(p: dict, s: int, e: int, spread=SPREAD, swap=SWAP):
    return bt.run(*_S["arrs"], _S["roll_high"][int(p["lookback"])], _S["trends"][p["trend"]], LOTS, PER_LEVEL, p["dip"], p["step"],
                  int(p["max_levels"]), p["tp"], p["sl"], 1 if p["trend"] else 0, spread, swap, s, e, _S["tmin"],
                  int(p["hold_h"] * 60), int(p["cool_h"] * 60))


def _stats(pnl, dd, maxlots) -> dict:
    gp, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    return dict(net=pnl.sum() * USDJPY, n=len(pnl), win=(pnl > 0).mean() if len(pnl) else np.nan,
                pf=gp / gl if gl > 0 else (np.inf if gp > 0 else 0.0), dd=dd * USDJPY,
                worst=pnl.min() * USDJPY if len(pnl) else 0.0, maxlots=maxlots)


def stage1(p: dict) -> dict:
    row = dict(p)
    for name, (s, e) in _S["periods"].items():
        pnl, dd, ml, _, _ = run_one(p, s, e)
        for k, v in _stats(pnl, dd, ml).items():
            row[f"{name}_{k}"] = v
    return row


def stage2(p: dict) -> dict:
    """15 期間・コスト悪化。"""
    nets, dds = [], []
    for s, e in _S["wins"]:
        pnl, dd, _, _, _ = run_one(p, s, e)
        nets.append(pnl.sum() * USDJPY)
        dds.append(dd * USDJPY)
    s, e = _S["periods"]["full"]
    pnl, dd, _, _, _ = run_one(p, s, e, SPREAD_BAD, SWAP_BAD)
    return dict(win_pos=int(np.sum(np.array(nets) > 0)), win_min=min(nets), win_dd=max(dds),
                bad_net=pnl.sum() * USDJPY, bad_dd=dd * USDJPY)


def configs() -> list[dict]:
    out = []
    keys = list(GRID)
    for vals in itertools.product(*GRID.values()):
        p = dict(zip(keys, vals))
        if p["max_levels"] == 1 and p["step"] != GRID["step"][0]:
            continue  # 1 段だけなら幅は無関係
        if p["cool_h"] and not (p["sl"] or p["hold_h"]):
            continue  # 損切りも時間切れも無ければクールダウンは無関係
        out.append(p)
    return out


def neighbors(p: dict) -> list[dict]:
    out = []
    for k, vals in GRID.items():
        i = vals.index(p[k])
        for j in (i - 1, i + 1):
            if 0 <= j < len(vals):
                q = dict(p)
                q[k] = vals[j]
                out.append(q)
    return out


def key(p: dict) -> tuple:
    q = dict(p)
    if q["max_levels"] == 1:
        q["step"] = GRID["step"][0]
    if not (q["sl"] or q["hold_h"]):
        q["cool_h"] = 0
    return tuple(q[k] for k in GRID)


def label(r) -> str:
    sl = "なし" if r["sl"] == 0 else f"${r['sl']:.0f}"
    hold = "なし" if r["hold_h"] == 0 else f"{r['hold_h']:.0f}時間"
    return (f"高値 {int(r['lookback'])}分 / 押し目 ${r['dip']:.0f} / 幅 ${r['step']:.0f} / 最大 {int(r['max_levels'])} 段 / 利確 ${r['tp']:.0f} / "
            f"損切り {sl} / {TREND_NAME[int(r['trend'])]} / 時間切れ {hold} / 休み {r['cool_h']:.0f}時間")


def man(x: float) -> str:
    return f"{x / 1e4:+,.1f}万"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    cfgs = configs()
    with mp.Pool(initializer=_init) as pool:
        res = pd.DataFrame(pool.map(stage1, cfgs, chunksize=64))
        idx = {key(r): i for i, r in enumerate(cfgs)}
        both = (res.h1_net > 0) & (res.h2_net > 0)
        res["both"] = both
        # 近傍合格率: パラメータを 1 つだけ 1 段階ずらした設定のうち前半・後半とも黒字の割合
        res["nb_rate"] = [np.mean([both.iloc[idx[key(q)]] for q in neighbors(p)]) for p in cfgs]
        cand = res[both & (res.full_pf > 1.1) & (res.full_n >= MIN_BASKETS) & (res.sl > 0)].copy()
        s2 = pd.DataFrame(pool.map(stage2, cand[list(GRID)].to_dict("records"), chunksize=8), index=cand.index)
    cand = cand.join(s2)
    res = res.join(s2)
    # 全件 (約 15 万行) は大きいので、15 期間・コスト悪化まで調べた候補だけ保存する
    cand.to_csv(OUT / "opt_candidates.csv", index=False, float_format="%.4f")

    robust = cand[(cand.win_pos >= 12) & (cand.bad_net > 0) & (cand.nb_rate >= 0.6)].copy()
    robust["ratio"] = robust.full_net / robust.full_dd
    # 時間切れ・休みが効かず成績が同じ設定はまとめる (近傍合格率が高いものを残す)
    robust = (robust.sort_values(["ratio", "nb_rate"], ascending=False)
              .drop_duplicates(["full_net", "full_dd", "full_n", "h1_net"]))
    pick = robust.iloc[0]

    # 旧初期値と、前半だけで選んだ場合 (ナイーブ最適化) の比較
    old = dict(lookback=60, dip=20.0, step=15.0, max_levels=3, tp=5.0, sl=30.0, trend=0, hold_h=0, cool_h=0)
    _init()
    old_row = stage1(old) | stage2(old)
    res["h1_ratio"] = res.h1_net / res.h1_dd.clip(lower=1)
    naive = (res[res.h1_n >= 15].sort_values("h1_ratio", ascending=False)
             .drop_duplicates(["h1_net", "h1_dd", "h1_n", "h2_net"]).head(10))

    # 要素別の合格率 (前半・後半とも黒字)
    factor_md = []
    for k, title in [("lookback", "直近高値の本数 (分)"), ("dip", "押し目"), ("sl", "損切り"), ("trend", "トレンドフィルタ"), ("hold_h", "時間切れ"),
                     ("cool_h", "損切り後の休み"), ("max_levels", "最大段数"), ("tp", "利確")]:
        g = res.groupby(k).agg(both=("both", "mean"), net=("full_net", "median"), dd=("full_dd", "median"))
        lines = [f"**{title}**", "", "| 値 | 前半・後半とも黒字 | 純損益 中央値 | 最大DD 中央値 |", "|---|---|---|---|"]
        for v, r in g.iterrows():
            name = TREND_NAME[int(v)] if k == "trend" else (("なし" if v == 0 else f"{v:g}"))
            lines.append(f"| {name} | {r.both:.0%} | {man(r.net)} | {r.dd / 1e4:,.1f}万 |")
        factor_md.append("\n".join(lines))

    # エクイティ曲線
    s, e = _S["periods"]["full"]
    m1 = _S["m1"]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for p, lab in [(old, "旧初期値"), (pick[list(GRID)].to_dict(), "新初期値")]:
        eq = run_one(p, s, e)[3][s:e]
        ax.plot(m1["time"].iloc[s:e], eq * USDJPY / 1e4, lw=1, label=lab)
    ax2 = ax.twinx()
    ax2.plot(m1["time"].iloc[s:e], m1["close"].iloc[s:e], color="0.75", lw=0.6)
    ax2.set_ylabel("金価格 (USD)")
    ax.set_ylabel("含み損込み損益 (万円, 0.01ロット x 3本/段)")
    ax.axhline(0, color="0.4", lw=0.5)
    ax.legend(loc="upper left")
    ax.set_title("GoldDipStackEA 旧初期値と新初期値 (2026-04-20〜09-25, 1分足)")
    fig.tight_layout()
    fig.savefig(OUT / "equity_opt.png", dpi=110)

    head = ("| 設定 | バスケット数 | 勝率 | PF | 純損益 | 最大DD | 最悪1回 | 前半 | 後半 | 15期間黒字 | 期間最悪DD "
            "| 近傍合格率 | コスト悪化時 |\n|" + "---|" * 13)

    def row(r) -> str:
        return (f"| {label(r)} | {int(r['full_n'])} | {r['full_win']:.0%} | {r['full_pf']:.2f} | {man(r['full_net'])} "
                f"| {r['full_dd'] / 1e4:,.1f}万 | {man(r['full_worst'])} | {man(r['h1_net'])} | {man(r['h2_net'])} "
                f"| {int(r['win_pos'])}/{N_WIN} | {r['win_dd'] / 1e4:,.1f}万 | {r.get('nb_rate', np.nan):.0%} "
                f"| {man(r['bad_net'])} |")

    nosl = res[res.sl == 0]
    md = f"""# GoldDipStackEA 改良版の探索

* データ: XAUUSD 1 分足 2026-03-26〜2026-09-25 (getdata-finance 無料サンプル, MIT)。取引は {TRADE_START:%Y-%m-%d} から (H4 SMA100 のウォームアップ後)。
  前半 = 〜{SPLIT:%Y-%m-%d} (下落局面)、後半 = それ以降 (上昇・反落)。
* {LOTS} ロット x {PER_LEVEL} 本/段。スプレッド ${SPREAD:.2f}、買いスワップ ${SWAP:.0f}/ロット/日 (水曜 3 倍)、USDJPY {USDJPY:.0f} 固定。
  0.50 ロット (Chieko さんと同じ) なら金額はすべて 50 倍。
* 探索 {len(res):,} 通り。前半・後半とも黒字 {int(res.both.sum()):,} 通り ({res.both.mean():.0%})。
  損切りなし {len(nosl):,} 通りのうち前半・後半とも黒字は {nosl.both.mean():.0%}。
* 選び方 (この順に絞る): 損切りあり / 前半・後半とも黒字 / PF > 1.1 / {MIN_BASKETS} バスケット以上 → 8 週間 x 15 期間のうち 12 期間以上黒字
  → コスト悪化 (スプレッド ${SPREAD_BAD:.2f}, スワップ ${SWAP_BAD:.0f}) でも黒字 → 近傍合格率 60% 以上 → 純損益 / 最大DD が最大。
  ここまで残ったのは {len(robust)} 通り (成績が同じものはまとめた数)。

## 新しい初期値

{head}
{row(pick)}

## 旧初期値 (比較)

{head}
{row(old_row | dict(nb_rate=np.nan))}

## 頑健な設定の上位 10 (純損益 / 最大DD 順)

{head}
""" + "\n".join(row(r) for _, r in robust.head(10).iterrows()) + f"""

## 前半だけで選んだ場合 (ナイーブ最適化) の後半

| 設定 | 前半 純損益 | 前半 最大DD | 後半 純損益 | 後半 最大DD |
|---|---|---|---|---|
""" + "\n".join(f"| {label(r)} | {man(r.h1_net)} | {r.h1_dd / 1e4:,.1f}万 | {man(r.h2_net)} | {r.h2_dd / 1e4:,.1f}万 |"
                for _, r in naive.iterrows()) + f"""

前半上位 10 件のうち後半も黒字: {int((naive.h2_net > 0).sum())} 件。

## 要素別

""" + "\n\n".join(factor_md) + """

![equity](equity_opt.png)
"""
    (OUT / "optimize.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
