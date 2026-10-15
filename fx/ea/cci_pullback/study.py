"""CCI 押し目買い EA の総当たり検証。

各 銘柄 × 下位足/上位足 で、フィルタと利確方法の全組み合わせを
前半・後半それぞれで集計し results/grid.csv.gz に保存する。

使い方:
    python3 study.py            # 総当たり (数分)
    python3 report.py           # results.md / equity.png を作成
"""
from __future__ import annotations

import itertools
import multiprocessing as mp
import pathlib

import pandas as pd

from cci_bt import SYMBOLS, TP_MODES, Params, build_frame, metrics, run
from data import SPLIT, bars

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "results"

COMBOS = [
    ("XAUUSD", "M5", "H1"), ("XAUUSD", "M5", "H4"),
    ("XAUUSD", "M15", "H1"), ("XAUUSD", "M15", "H4"),
    ("EURUSD", "M15", "H1"), ("EURUSD", "M15", "H4"),
    ("GBPUSD", "M15", "H1"), ("GBPUSD", "M15", "H4"),
]

GRID = dict(
    env=["structure", "ema", "none"],
    env_cci=[-1e9, 0.0, 50.0, 100.0],
    candle=[0, 1],
    ema_touch=[0, 1],
    fib=[0, 1],
    max_depth=[0.0, 200.0],
    hard_sl=[0, 1],
    tp_mode=list(TP_MODES),
)


def summarize(tr: pd.DataFrame, split: pd.Timestamp) -> dict:
    out = {}
    for name, part in (("all", tr), ("a", tr[tr.entry_time < split]), ("b", tr[tr.entry_time >= split])):
        for k, v in metrics(part).items():
            out[f"{name}_{k}"] = v
    return out


def _work(combo):
    sym, ltf, htf = combo
    L, H = bars(sym, ltf), bars(sym, htf)
    d = build_frame(L, H, Params(ltf=ltf, htf=htf))
    rows = []
    keys = list(GRID)
    for vals in itertools.product(*GRID.values()):
        kw = dict(zip(keys, vals))
        if kw["env"] == "none" and kw["fib"] == 1:
            continue  # フィボは上位足スイングが前提
        p = Params(ltf=ltf, htf=htf, **kw)
        tr = run(d, p, SYMBOLS[sym])
        rows.append(dict(symbol=sym, ltf=ltf, htf=htf, **kw, **summarize(tr, SPLIT[sym])))
    print(f"{sym} {ltf}/{htf}: {len(rows)} 通り", flush=True)
    return rows


def main() -> None:
    OUT.mkdir(exist_ok=True)
    with mp.Pool(min(len(COMBOS), mp.cpu_count())) as pool:
        res = pool.map(_work, COMBOS)
    df = pd.DataFrame([r for rows in res for r in rows])
    df.to_csv(OUT / "grid.csv.gz", index=False, float_format="%.4g")
    print(f"{len(df)} 通り → {OUT / 'grid.csv.gz'}")


if __name__ == "__main__":
    main()
