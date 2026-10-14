"""パラメータ探索: 前半 (IS) で最適化し、後半 (OOS) で検証する。

使い方:
    python optimize.py --data ../data/XAUUSD_1m_2026-03-26_2026-09-25.csv.gz --out ../results
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import time

import numpy as np
import pandas as pd

from nanpin_bt import (Backtester, Market, Params, FILTERS, DIR_NAMES, load_m1)

# 探索範囲 (倍率は仕様どおり 1.2 固定)
SPACE = {
    "direction": [0, 1, 2],
    "init_lot": [0.01, 0.02, 0.03, 0.05, 0.10],
    "grid_step": [5, 10, 15, 20, 30, 50, 75, 100],
    "max_pos": [5, 8, 10, 15, 20],
    "tp_move": [5, 10, 20, 40],
    "stop_pct": [0.0, 0.05, 0.10, 0.15],
    "filter_id": [0, 1, 2],
}
MULT = 1.2
COOLDOWN = 1440
MAX_DD = 20.0

# 期間 (UTC)。開始はトレンドフィルタ (H4 SMA100 = 400 時間) のウォームアップ後
WARMUP_END = "2026-04-20"
SPLIT = "2026-07-06"
END = "2026-09-26"


def make_grid():
    keys = list(SPACE)
    combos = list(itertools.product(*[SPACE[k] for k in keys]))
    cfg = pd.DataFrame(combos, columns=keys)
    grid = np.column_stack([
        cfg.direction, cfg.init_lot, np.full(len(cfg), MULT), cfg.grid_step, cfg.max_pos,
        cfg.tp_move, cfg.stop_pct, np.full(len(cfg), COOLDOWN), cfg.filter_id,
    ]).astype(np.float64)
    return cfg, grid


def score(df, prefix):
    """利益 / 最大DD (DD は最低 1% として扱う)。"""
    return df[f"{prefix}return_pct"] / df[f"{prefix}maxdd_pct"].clip(lower=1.0)


def feasible(df, prefix):
    return ((df[f"{prefix}maxdd_pct"] <= MAX_DD) & (df[f"{prefix}profit_jpy"] > 0)
            & (df[f"{prefix}n_stopout"] == 0))


def label(row):
    return (f"{DIR_NAMES[int(row.direction)]} / 初期{row.init_lot:.2f}lot / 幅${row.grid_step:g} / "
            f"最大{int(row.max_pos)}本 / 利確${row.tp_move:g}相当 / "
            f"損切り{'なし' if row.stop_pct == 0 else f'{row.stop_pct*100:.0f}%'} / "
            f"フィルタ:{FILTERS[int(row.filter_id)][0]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(os.path.dirname(__file__), "..", "data", "XAUUSD_1m_2026-03-26_2026-09-25.csv.gz"))
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "results"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    df = load_m1(args.data)
    bt = Backtester(df, Market())
    cfg, grid = make_grid()
    print(f"{len(cfg)} 通りを探索します")

    periods = {"is_": (WARMUP_END, SPLIT), "oos_": (SPLIT, END), "full_": (WARMUP_END, END)}
    res = cfg.copy()
    for prefix, (s, e) in periods.items():
        t = time.time()
        r = bt.run_grid(grid, s, e)
        r.columns = [prefix + c for c in r.columns]
        res = pd.concat([res, r], axis=1)
        print(f"{prefix}: {time.time() - t:.1f}s")

    for p in ("is_", "oos_", "full_"):
        res[p + "ok"] = feasible(res, p)
        res[p + "score"] = score(res, p)
    res["label"] = res.apply(label, axis=1)
    res.to_csv(os.path.join(args.out, "all_results.csv.gz"), index=False, float_format="%.4f")

    # フォールド A: 前半で選んで後半で検証 / フォールド B: 逆
    summary = {"n_configs": len(res)}
    for name, a, b in (("A", "is_", "oos_"), ("B", "oos_", "is_")):
        sel = res[res[a + "ok"]].sort_values(a + "score", ascending=False)
        summary[f"fold{name}_n_opt_ok"] = int(len(sel))
        summary[f"fold{name}_n_both_ok"] = int(sel[b + "ok"].sum())
        top = sel.head(30)
        summary[f"fold{name}_top30_val_ok"] = int(top[b + "ok"].sum())
        top.to_csv(os.path.join(args.out, f"fold{name}_top.csv"), index=False, float_format="%.4f")
    # 全期間とも条件を満たす設定
    both = res[res.is_ok & res.oos_ok & res.full_ok].copy()
    both["min_score"] = np.minimum(both.is_score, both.oos_score)
    both = both.sort_values("min_score", ascending=False)
    both.to_csv(os.path.join(args.out, "robust.csv"), index=False, float_format="%.4f")
    summary["n_all_ok"] = int(len(both))
    # 区分ごとの集計
    for col in ("direction", "filter_id", "stop_pct", "grid_step", "max_pos", "init_lot", "tp_move"):
        g = res.groupby(col).agg(is_ok=("is_ok", "mean"), oos_ok=("oos_ok", "mean"),
                                 both_ok=("is_ok", lambda s: (s & res.loc[s.index, "oos_ok"]).mean()))
        g.to_csv(os.path.join(args.out, f"by_{col}.csv"), float_format="%.4f")
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
