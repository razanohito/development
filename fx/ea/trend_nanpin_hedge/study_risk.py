"""「1 回の大負けを小さくする」ための比較 (峻さん 2026-11-02)。 python3 study_risk.py

CCI ゼロ抜け・最大 4 本 (3・4 本目は逆方向) を土台に、次の 3 つを組み合わせて比べる。
  1. ロット倍率   1.5 (ご指定) / 1.2 / 1.0
  2. 1 サイクルの損失上限 (超えたら全決済)  なし / 50 / 100 / 200 ドル (= 0.75 / 1.5 / 3 万円)
  3. 強トレンド時は入らない  なし / H1 ADX > 40 / > 30 / > 25 のとき入らない
指標は 損益・最大ドローダウン・最悪の 1 回 (1 サイクルの損失)。
出力: results/grid_risk.csv, results/compare_risk.md, results/equity_risk.png
"""
from __future__ import annotations

import itertools
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from signals import SPECS, adx_block  # noqa: E402
from study_cci import DATASETS, NAMES, prepare  # noqa: E402
from tnh_bt import simulate, stats  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "results"

CCI_N = [14, 20]
STEPS = [("fix", 50), ("fix", 300), ("fix", 500), ("fix", 700), ("atr", 0.5), ("atr", 0.75), ("atr", 1.0)]
TP_R = [0.2, 0.5, 1.0]
HTP_R = [1.0, 2.0]
REV_R = [0.2, 0.5]
MULT = [1.5, 1.2, 1.0]
STOP = [0.0, 50.0, 100.0, 200.0]   # ドル
ADX = [0, 40, 30, 25]              # 0 = フィルタなし

BASE = dict(lot0=0.01, max_pos=4, hedge_from=3, after=1, rev_from=4, lot_step=0.01)
USER = dict(cci_n=14, smode="fix", step=50, tp_r=0.5, htp_r=1.0, rev_r=0.4, mult=1.5, stop=0.0, adx=0)
KEY = ["cci_n", "smode", "step", "tp_r", "htp_r", "rev_r", "mult", "stop", "adx"]


def run(prep, blocks, cfg, **kw):
    df, arr, sigs, atr, half = prep
    spec = SPECS["XAUUSD"]
    f = dict(BASE, **kw)
    sig = sigs[(cfg["cci_n"], False)].copy()
    if cfg["adx"]:
        sig[blocks[cfg["adx"]]] = 0
    sa = np.full(len(df), float(cfg["step"])) if cfg["smode"] == "fix" else np.maximum(atr * cfg["step"], 1.0)
    o, h, l, c = arr
    eq, cyc = simulate(o, h, l, c, sig, spec["pip"], spec["spread"], spec["cs"], f["lot0"], float(cfg["mult"]),
                       f["max_pos"], sa, f["hedge_from"], f["after"], float(cfg["htp_r"]), float(cfg["tp_r"]),
                       f["rev_from"], float(cfg["rev_r"]), float(cfg["stop"]), f["lot_step"])
    st = stats(eq, cyc)
    st["h1"] = float(eq[half - 1]) * 150
    st["h2"] = st["profit_jpy"] - st["h1"]
    return eq, cyc, st


def fmt(v):
    return f"{v / 1e4:+.1f}万"


def label(c):
    s = f"{c['step']:g}pips" if c["smode"] == "fix" else f"ATR×{c['step']:g}"
    stop = "なし" if not c["stop"] else f"{c['stop'] * 150 / 1e4:g}万円"
    adx = "なし" if not c["adx"] else f"ADX>{c['adx']}"
    return (f"CCI{c['cci_n']} / {s} / 利確×{c['tp_r']:g} 救済×{c['htp_r']:g} 反転×{c['rev_r']:g} / "
            f"倍率{c['mult']:g} / 損失上限 {stop} / 強トレンド停止 {adx}")


HEAD = ("| 設定 | 回数 | 損益 | 前半 / 後半 | 最大DD | 最悪の1回 | PF | 勝率 |\n"
        "|---|---|---|---|---|---|---|---|")


def row(lab, st):
    return (f"| {lab} | {st['cycles']} | {fmt(st['profit_jpy'])} | {fmt(st['h1'])} / {fmt(st['h2'])} | "
            f"{fmt(-st['maxdd_jpy'])} | {fmt(st['worst_jpy'])} | {st['pf']:.2f} | {st['win'] * 100:.0f}% |")


def main() -> None:
    preps = {k: prepare(k) for k in DATASETS}
    blocks = {k: {t: adx_block(p[0], t) for t in ADX if t} for k, p in preps.items()}
    rows = []
    for k, prep in preps.items():
        for vals in itertools.product(CCI_N, STEPS, TP_R, HTP_R, REV_R, MULT, STOP, ADX):
            n, (sm, sv), tp, htp, rv, mu, sp, ax = vals
            cfg = dict(cci_n=n, smode=sm, step=sv, tp_r=tp, htp_r=htp, rev_r=rv, mult=mu, stop=sp, adx=ax)
            _, _, st = run(prep, blocks[k], cfg)
            rows.append(dict(data=k, **cfg, **st))
        print(k, "done")
    g = pd.DataFrame(rows)
    g.to_csv(OUT / "grid_risk.csv", index=False, float_format="%.4g")

    a = g[g.data == "gold_m1"].set_index(KEY)
    b = g[g.data == "gold_m15"].set_index(KEY)
    j = a.join(b, rsuffix="_ref")
    j["worst2"] = np.minimum(j.worst_jpy, j.worst_jpy_ref)
    j["dd2"] = np.maximum(j.maxdd_jpy, j.maxdd_jpy_ref)
    good = j[(j.h1 > 0) & (j.h2 > 0) & (j.profit_jpy_ref > 0) & (j.cycles >= 100)]
    md = ["# 「1 回の大負けを小さくする」比較 (CCI ゼロ抜け, 最大 4 本, 3・4 本目は逆方向, 0.01 ロット開始, 1 ドル = 150 円)\n",
          f"全 {len(a)} 通り。2026 年の前半・後半とも黒字かつ 参考データ (2019〜22) でも黒字: {len(good)} 通り。\n"]

    # 1) 対策ごとの効果 (他の設定は全部込みで、中央値と黒字の割合)
    md.append("## 対策ごとの効果 (2026 年, 他の設定すべての中央値)\n")
    md.append("| 対策 | 値 | 損益の中央値 | 最大DDの中央値 | 最悪の1回の中央値 | 前後半とも黒字の割合 |\n|---|---|---|---|---|---|")
    x = g[g.data == "gold_m1"].assign(both=lambda d: (d.h1 > 0) & (d.h2 > 0))
    for col, name, f in (("mult", "ロット倍率", lambda v: f"{v:g}"),
                         ("stop", "1サイクル損失上限", lambda v: "なし" if not v else f"{v * 150 / 1e4:g}万円"),
                         ("adx", "強トレンド時は入らない", lambda v: "なし" if not v else f"H1 ADX>{v}")):
        for v, d in x.groupby(col, sort=False):
            md.append(f"| {name} | {f(v)} | {fmt(d.profit_jpy.median())} | {fmt(-d.maxdd_jpy.median())} | "
                      f"{fmt(d.worst_jpy.median())} | {d.both.mean() * 100:.0f}% |")

    # 2) 推奨: 両データで最悪の 1 回が -1.5 万円以内のうち、利益/最大DD (悪い方のデータ) が最大
    cand = good[good.worst2 >= -15000]
    if not len(cand):
        cand = good
    cand = cand.assign(score=np.minimum(cand.profit_jpy / cand.maxdd_jpy, cand.profit_jpy_ref / cand.maxdd_jpy_ref))
    cand = cand.sort_values("score", ascending=False)
    rec = dict(zip(KEY, cand.index[0]))
    rec = {k: (v.item() if hasattr(v, "item") else v) for k, v in rec.items()}
    # 大負け対策なしで一番良かったもの (比較用)
    raw = good.xs((1.5, 0.0, 0), level=["mult", "stop", "adx"], drop_level=False)
    raw = raw.assign(score=np.minimum(raw.profit_jpy / raw.maxdd_jpy, raw.profit_jpy_ref / raw.maxdd_jpy_ref))
    best_raw = dict(zip(KEY, raw.sort_values("score", ascending=False).index[0])) if len(raw) else None

    md.append("\n## 推奨の選び方\n")
    md.append("2026 年の前半・後半とも黒字、参考データ (2019〜22) でも黒字、100 回以上取引、"
              "両データとも「最悪の 1 回」が -1.5 万円以内、のうち 両データの「損益 ÷ 最大DD」の悪い方が最大のもの。\n")
    md.append(f"推奨: {label(rec)}\n")
    md.append("\n## 条件を満たした上位 10 通り\n")
    md.append("| 設定 | 2026 損益 | 2026 最大DD | 2026 最悪の1回 | 参考 損益 | 参考 最大DD | 参考 最悪の1回 |\n|---|---|---|---|---|---|---|")
    for idx, r in cand.head(10).iterrows():
        c = dict(zip(KEY, idx))
        md.append(f"| {label(c)} | {fmt(r.profit_jpy)} | {fmt(-r.maxdd_jpy)} | {fmt(r.worst_jpy)} | "
                  f"{fmt(r.profit_jpy_ref)} | {fmt(-r.maxdd_jpy_ref)} | {fmt(r.worst_jpy_ref)} |")

    curves = {}
    for k, prep in preps.items():
        md.append(f"\n## {NAMES[k]}\n")
        md.append("### 推奨から 1 つずつ外した場合\n")
        md.append(HEAD)
        variants = [("推奨 (全部入り)", rec),
                    ("倍率だけ 1.5 に戻す", dict(rec, mult=1.5)),
                    ("損失上限を外す", dict(rec, stop=0.0)),
                    ("強トレンド停止を外す", dict(rec, adx=0)),
                    ("3 つとも外す", dict(rec, mult=1.5, stop=0.0, adx=0))]
        if best_raw:
            variants.append(("対策なしで一番良かった設定", best_raw))
        variants.append(("ご指定 50pips (対策なし)", USER))
        variants.append(("ご指定 50pips + 推奨の対策", dict(USER, mult=rec["mult"], stop=rec["stop"], adx=rec["adx"])))
        for lab, cfg in variants:
            eq, cyc, st = run(prep, blocks[k], cfg)
            md.append(row(lab, st))
            curves[(k, lab)] = (prep[0].time.values, eq * 150)
        md.append("\n### 推奨の損失上限を変えた場合\n")
        md.append(HEAD)
        for sp in STOP:
            md.append(row("なし" if not sp else f"{sp * 150 / 1e4:g}万円", run(prep, blocks[k], dict(rec, stop=sp))[2]))
    (OUT / "compare_risk.md").write_text("\n".join(md), encoding="utf-8")

    fig, axes = plt.subplots(2, 1, figsize=(10, 7))
    for ax, k in zip(axes, DATASETS):
        for lab, color, en in (("推奨 (全部入り)", "#2a6fdb", "recommended (with risk controls)"),
                               ("3 つとも外す", "#999999", "same, no risk controls"),
                               ("ご指定 50pips (対策なし)", "#d9534f", "user 50pips")):
            t, e = curves[(k, lab)]
            ax.plot(t, e / 1e4, color=color, lw=1.2, label=en)
        ax.axhline(0, color="#bbb", lw=0.8)
        ax.set_title(k + (" (CCI on M5)" if k == "gold_m1" else " (CCI on M15, reference)"))
        ax.set_ylabel("JPY (x10,000)")
        ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "equity_risk.png", dpi=110)
    print((OUT / "compare_risk.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
