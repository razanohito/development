"""study.py の結果から、間隔の比較表・ロジック別の効果・推奨設定の資産曲線を作る。

    python3 study.py && python3 compare.py   → results/compare.md, results/equity.png
"""
from __future__ import annotations

import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from signals import signals  # noqa: E402
from study import DATASETS, OUT, prepare, run_one, step_array  # noqa: E402
from tnh_bt import stats  # noqa: E402

# 推奨 (3 データすべてでプラス、前後半 6 区間中 5 区間プラス、利益/最大DD 最大)
REC = dict(entry="rsi", smode="atr", step=0.5, tp_r=0.2, htp_r=0.5, rev_r=0.5, after=0)
# ユーザー指定そのまま (50pips, 未指定の部分は利確 25pips, 救済利確 50pips, 反転 20pips)
USER = dict(entry="rsi", smode="fix", step=50, tp_r=0.5, htp_r=1.0, rev_r=0.4, after=0)
NAMES = {"gold_m1": "金 1分足 2026/2〜9", "gold_m15": "金 15分足 2019〜2022/3",
         "eurusd_m15": "ユーロドル 15分足 2012/11〜2022/3"}


def fmt(v):
    return f"{v / 1e4:+.1f}万"


def run_cfg(prep, cfg, **kw):
    df, arr, sigs, spec, steps, half, atr = prep
    sig = sigs[cfg["entry"]] if "seed" not in kw else signals(df, "random", seed=kw.pop("seed"))
    sa = step_array(len(df), atr, cfg["smode"], cfg["step"])
    eq, cyc = run_one(arr, sig, spec, sa, cfg["tp_r"], cfg["htp_r"], cfg["rev_r"], cfg["after"], **kw)
    st = stats(eq, cyc)
    st["h1"] = float(eq[half - 1]) * 150
    st["h2"] = st["profit_jpy"] - st["h1"]
    return eq, cyc, st


def row(label, st):
    return (f"| {label} | {st['cycles']} | {fmt(st['profit_jpy'])} | {fmt(st['h1'])} / {fmt(st['h2'])} | "
            f"{fmt(-st['maxdd_jpy'])} | {st['pf']:.2f} | {st['win'] * 100:.0f}% | {st['le2'] * 100:.0f}% | "
            f"{st['ge4'] * 100:.0f}% | {fmt(st['worst_jpy'])} |")


HEAD = ("| 設定 | 回数 | 損益 | 前半 / 後半 | 最大DD | PF | 勝率 | 1〜2本で決済 | 4本以上 | 最悪の1回 |\n"
        "|---|---|---|---|---|---|---|---|---|---|")


def main() -> None:
    g = pd.read_csv(OUT / "grid.csv")
    md = ["# 比較結果 (0.01 ロット開始, 1 ドル = 150 円)\n"]
    preps = {k: prepare(k) for k in DATASETS}
    curves = {}
    for key, prep in preps.items():
        md.append(f"\n## {NAMES[key]}\n")
        md.append("### 間隔だけを変えた比較 (それ以外は推奨設定)\n")
        md.append(HEAD)
        steps = DATASETS[key][3]
        for smode, vals in (("fix", steps), ("atr", [0.25, 0.5, 0.75, 1.0, 1.5])):
            for v in vals:
                cfg = dict(REC, smode=smode, step=v)
                _, _, st = run_cfg(prep, cfg)
                label = f"{v:g} pips" if smode == "fix" else f"日足ATR×{v:g}"
                md.append(row(label, st))
        x = g[g.data == key]
        md.append("\n間隔ごとの全組み合わせ (エントリー3種×利確などの他の設定 216 通り) のうち、"
                  "前半・後半ともプラスだった割合:\n")
        x = x.assign(both=(x.h1_jpy > 0) & (x.h2_jpy > 0),
                     sk=np.where(x.smode == "fix", x.step.map(lambda v: f"{v:g}pips"),
                                 x.step.map(lambda v: f"ATR×{v:g}")))
        r = x.groupby("sk", sort=False).both.mean()
        md.append(" / ".join(f"{k} {v * 100:.0f}%" for k, v in r.items()) + "\n")

        md.append("### ロジック別の効果\n")
        md.append(HEAD)
        for name, cfg in (("推奨", REC), ("ユーザー指定 (50pips)", USER)):
            for lab, kw in (("ロジック1+2+反転決済 (完成形)", {}),
                            ("救済なし (5本とも順張りナンピン)", dict(hedge_from=99)),
                            ("反転決済なし", dict(rev_from=99)),
                            ("ロジック1のみ", dict(hedge_from=99, rev_from=99)),
                            ("+安全装置 1サイクル -1.5万円", dict(max_loss=100.0)),
                            ("+安全装置 1サイクル -4.5万円", dict(max_loss=300.0))):
                eq, cyc, st = run_cfg(prep, cfg, **kw)
                md.append(row(f"{name}: {lab}", st))
                if lab.startswith("ロジック1+2") :
                    curves[(key, name)] = (prep[0].time.values, eq * 150)
        md.append("\n### エントリーをランダムにした場合 (推奨のお金の管理のみ, 20 通り)\n")
        res = [run_cfg(prep, REC, seed=s)[2]["profit_jpy"] for s in range(20)]
        md.append(f"プラス {sum(v > 0 for v in res)}/20, 中央値 {fmt(float(np.median(res)))}, "
                  f"最小 {fmt(min(res))}, 最大 {fmt(max(res))}\n")
    (OUT / "compare.md").write_text("\n".join(md), encoding="utf-8")

    fig, axes = plt.subplots(3, 1, figsize=(10, 10))
    for ax, key in zip(axes, DATASETS):
        for name, color in (("推奨", "#2a6fdb"), ("ユーザー指定 (50pips)", "#d9534f")):
            t, e = curves[(key, name)]
            ax.plot(t, e / 1e4, color=color, lw=1.2, label="recommended" if name == "推奨" else "user 50pips")
        ax.axhline(0, color="#999", lw=0.8)
        ax.set_title(key)
        ax.set_ylabel("JPY (x10,000)")
        ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "equity.png", dpi=110)
    print((OUT / "compare.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
