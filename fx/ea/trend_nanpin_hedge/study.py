"""ナンピン間隔などを総当たりで比べる。 python3 study.py

データ 3 本 (各前半/後半にも分ける):
  gold_m1   : XAUUSD 1 分足 2026-02-01〜2026-09-25
  gold_m15  : XAUUSD 15 分足 2019-01-01〜2022-03-04
  eurusd_m15: EURUSD 15 分足 2012-11-14〜2022-03-04
固定 (ユーザー指定): 0.01 ロット開始, 倍率 1.5, 最大 5 本, 3 本目で逆方向, 4 本以上は反転で決済。
金額は 1 ドル = 150 円で円換算。
"""
from __future__ import annotations

import itertools
import pathlib

import numpy as np
import pandas as pd

from signals import SPECS, daily_atr_pips, load, signals
from tnh_bt import simulate, stats

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "results"

DATASETS = {
    "gold_m1": ("XAUUSD_m1", "XAUUSD", None, [20, 30, 50, 80, 100, 150, 200, 300, 500, 700, 1000]),
    "gold_m15": ("XAUUSD_m15", "XAUUSD", "2019-01-01", [20, 30, 50, 80, 100, 150, 200, 300, 500, 700, 1000]),
    "eurusd_m15": ("EURUSD_m15", "EURUSD", None, [10, 15, 20, 30, 50, 80, 100, 150, 200]),
}
ATR_K = [0.25, 0.5, 0.75, 1.0, 1.5]  # 間隔 = 日足 ATR(14) × 係数
TP_R = [0.2, 0.5, 1.0]       # バスケット利確 = 間隔 × 比率
HTP_R = [0.5, 1.0, 2.0]      # 逆方向ポジションの利確 = 間隔 × 比率
REV_R = [0.2, 0.5]           # 4 本以上の反転幅 = 間隔 × 比率
AFTER = [0, 1]               # 4・5 本目: 0=元の方向にナンピン, 1=逆方向を続ける
ENTRY = ["rsi", "breakout", "random"]

FIXED = dict(lot0=0.01, mult=1.5, max_pos=5, hedge_from=3, rev_from=4, lot_step=0.01)


def step_array(n, atr, mode, val):
    return np.full(n, float(val)) if mode == "fix" else np.maximum(atr * val, 1.0)


def run_one(arr, sig, spec, step_arr, tp_r, htp_r, rev_r, after, max_loss=0.0, **kw):
    o, h, l, c = arr
    f = dict(FIXED, **kw)
    return simulate(o, h, l, c, sig, spec["pip"], spec["spread"], spec["cs"],
                    f["lot0"], f["mult"], f["max_pos"], step_arr, f["hedge_from"],
                    after, float(htp_r), float(tp_r), f["rev_from"],
                    float(rev_r), float(max_loss), f["lot_step"])


def prepare(key):
    name, sym, start, steps = DATASETS[key]
    df = load(name)
    if start:
        df = df[df.time >= start].reset_index(drop=True)
    sigs = {m: signals(df, m) for m in ENTRY}
    arr = tuple(df[k].values for k in ("open", "high", "low", "close"))
    half = len(df) // 2
    atr = daily_atr_pips(df, SPECS[sym]["pip"])
    return df, arr, sigs, SPECS[sym], steps, half, atr


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rows = []
    for key in DATASETS:
        df, arr, sigs, spec, steps, half, atr = prepare(key)
        modes = [("fix", v) for v in steps] + [("atr", k) for k in ATR_K]
        for entry, (smode, step), tp_r, htp_r, rev_r, after in itertools.product(
                ENTRY, modes, TP_R, HTP_R, REV_R, AFTER):
            eq, cyc = run_one(arr, sigs[entry], spec, step_array(len(df), atr, smode, step),
                              tp_r, htp_r, rev_r, after)
            st = stats(eq, cyc)
            st["h1_jpy"] = float(eq[half - 1]) * 150
            st["h2_jpy"] = st["profit_jpy"] - st["h1_jpy"]
            rows.append(dict(data=key, entry=entry, smode=smode, step=step, tp_r=tp_r, htp_r=htp_r, rev_r=rev_r,
                             after=after, **st))
        print(key, "done")
    g = pd.DataFrame(rows)
    g.to_csv(OUT / "grid.csv", index=False, float_format="%.4g")
    print(len(g), "rows")


if __name__ == "__main__":
    main()
