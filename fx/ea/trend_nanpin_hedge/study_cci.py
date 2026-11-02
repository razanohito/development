"""峻さんの回答 (2026-11-02) を反映した比較。 python3 study_cci.py

  - 3・4 本目は逆方向、5 本目はなし (最大 4 本, 4 本目で反転決済が効く)
  - ゴールド 5 分足、初回エントリーは CCI の 0 ライン抜け
データ:
  gold_m1   : XAUUSD 1 分足 2026-02-01〜2026-09-25 を土台に、CCI は 5 分足で判定
  gold_m15  : 参考。XAUUSD 15 分足 2019〜2022/3 (5 分足が無いので 15 分足の CCI で代用)
出力: results/grid_cci.csv, results/compare_cci.md, results/equity_cci.png
"""
from __future__ import annotations

import itertools
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from signals import SPECS, daily_atr_pips, load, signals  # noqa: E402
from tnh_bt import simulate, stats  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "results"

DATASETS = {
    "gold_m1": ("XAUUSD_m1", None, "5min"),
    "gold_m15": ("XAUUSD_m15", "2019-01-01", "15min"),
}
NAMES = {"gold_m1": "ゴールド 2026/2〜9 (5分足CCI, 1分足で約定)",
         "gold_m15": "参考: ゴールド 2019〜2022/3 (15分足CCIで代用)"}
FIX = [20, 30, 50, 80, 100, 150, 200, 300, 500, 700, 1000]
ATR_K = [0.1, 0.15, 0.25, 0.5, 0.75, 1.0]
TP_R = [0.2, 0.5, 1.0]
HTP_R = [0.5, 1.0, 2.0]
REV_R = [0.2, 0.5]
CCI_N = [14, 20]
FILT = [False, True]

FIXED = dict(lot0=0.01, mult=1.5, max_pos=4, hedge_from=3, after=1, rev_from=4, lot_step=0.01)

# 推奨 (grid_cci.csv から選んだもの。選び方は compare_cci.md)
REC = None  # main() の中で選ぶ
USER = dict(cci_n=14, filt=False, smode="fix", step=50, tp_r=0.5, htp_r=1.0, rev_r=0.4)


def run(prep, cfg, seed=None, **kw):
    df, arr, sigs, atr, half = prep
    spec = SPECS["XAUUSD"]
    f = dict(FIXED, **kw)
    sig = sigs[(cfg["cci_n"], cfg["filt"])] if seed is None else signals(df, "random", seed=seed)
    sa = np.full(len(df), float(cfg["step"])) if cfg["smode"] == "fix" else np.maximum(atr * cfg["step"], 1.0)
    o, h, l, c = arr
    eq, cyc = simulate(o, h, l, c, sig, spec["pip"], spec["spread"], spec["cs"], f["lot0"], f["mult"],
                       f["max_pos"], sa, f["hedge_from"], f["after"], float(cfg["htp_r"]), float(cfg["tp_r"]),
                       f["rev_from"], float(cfg["rev_r"]), float(kw.get("max_loss", 0.0)), f["lot_step"])
    st = stats(eq, cyc)
    st["h1"] = float(eq[half - 1]) * 150
    st["h2"] = st["profit_jpy"] - st["h1"]
    return eq, cyc, st


def prepare(key):
    name, start, tf = DATASETS[key]
    df = load(name)
    if start:
        df = df[df.time >= start].reset_index(drop=True)
    sigs = {(n, fl): signals(df, "cci", tf=tf, cci_n=n, ema_filter=fl) for n in CCI_N for fl in FILT}
    arr = tuple(df[k].values for k in ("open", "high", "low", "close"))
    return df, arr, sigs, daily_atr_pips(df, SPECS["XAUUSD"]["pip"]), len(df) // 2


def fmt(v):
    return f"{v / 1e4:+.1f}万"


HEAD = ("| 設定 | 回数 | 損益 | 前半 / 後半 | 最大DD | PF | 勝率 | 1〜2本で決済 | 4本 | 最悪の1回 |\n"
        "|---|---|---|---|---|---|---|---|---|---|")


def row(label, st):
    return (f"| {label} | {st['cycles']} | {fmt(st['profit_jpy'])} | {fmt(st['h1'])} / {fmt(st['h2'])} | "
            f"{fmt(-st['maxdd_jpy'])} | {st['pf']:.2f} | {st['win'] * 100:.0f}% | {st['le2'] * 100:.0f}% | "
            f"{st['ge4'] * 100:.0f}% | {fmt(st['worst_jpy'])} |")


def label(cfg):
    s = f"{cfg['step']:g}pips" if cfg["smode"] == "fix" else f"ATR×{cfg['step']:g}"
    return s


def main() -> None:
    OUT.mkdir(exist_ok=True)
    preps = {k: prepare(k) for k in DATASETS}
    rows = []
    for key, prep in preps.items():
        modes = [("fix", v) for v in FIX] + [("atr", k) for k in ATR_K]
        for n, fl, (sm, st_), tp, htp, rv in itertools.product(CCI_N, FILT, modes, TP_R, HTP_R, REV_R):
            cfg = dict(cci_n=n, filt=fl, smode=sm, step=st_, tp_r=tp, htp_r=htp, rev_r=rv)
            _, _, st = run(prep, cfg)
            rows.append(dict(data=key, **cfg, **st))
        print(key, "done")
    g = pd.DataFrame(rows)
    g.to_csv(OUT / "grid_cci.csv", index=False, float_format="%.4g")

    # 推奨: 2026 の前半・後半とも黒字、かつ 参考データでも黒字のうち、2026 の 利益/最大DD が最大
    key = ["cci_n", "filt", "smode", "step", "tp_r", "htp_r", "rev_r"]
    a = g[g.data == "gold_m1"].set_index(key)
    b = g[g.data == "gold_m15"].set_index(key)
    j = a.join(b, rsuffix="_ref")
    ok = j[(j.h1 > 0) & (j.h2 > 0) & (j.profit_jpy_ref > 0) & (j.cycles >= 100)]
    ok = ok.assign(ratio=ok.profit_jpy / ok.maxdd_jpy).sort_values("ratio", ascending=False)
    md = ["# CCI ゼロ抜け版の比較 (最大 4 本, 3・4 本目は逆方向, 0.01 ロット開始, 1 ドル = 150 円)\n",
          f"全 {len(a)} 通り (間隔 17 × 利確 3 × 救済利確 3 × 反転 2 × CCI 期間 2 × トレンドフィルタ 2) のうち、"
          f"2026 年の前半・後半とも黒字: {int(((a.h1 > 0) & (a.h2 > 0)).sum())} 通り、"
          f"さらに参考データ (2019〜22) でも黒字: {len(ok)} 通り。\n"]
    global REC
    if len(ok):
        REC = dict(zip(key, ok.index[0]))
    else:
        REC = dict(zip(key, a.assign(r=a.profit_jpy / a.maxdd_jpy).r.idxmax()))
    REC = {k: (v.item() if hasattr(v, "item") else v) for k, v in REC.items()}
    md.append(f"推奨: CCI({REC['cci_n']}) {'+ H1 EMA200 の向きだけ' if REC['filt'] else 'フィルタなし'}, "
              f"間隔 {label(REC)}, 利確 間隔×{REC['tp_r']:g}, 救済利確 ×{REC['htp_r']:g}, 反転 ×{REC['rev_r']:g}\n")
    if len(ok):
        md.append("\n## 条件を満たした上位 10 通り (2026 / 参考)\n")
        md.append("| CCI | フィルタ | 間隔 | 利確 | 救済利確 | 反転 | 2026 損益 | 2026 DD | 参考 損益 | 参考 DD |\n|---|---|---|---|---|---|---|---|---|---|")
        for idx, r in ok.head(10).iterrows():
            c = dict(zip(key, idx))
            md.append(f"| {c['cci_n']} | {'EMA' if c['filt'] else 'なし'} | {label(c)} | {c['tp_r']:g} | {c['htp_r']:g} | "
                      f"{c['rev_r']:g} | {fmt(r.profit_jpy)} | {fmt(-r.maxdd_jpy)} | {fmt(r.profit_jpy_ref)} | {fmt(-r.maxdd_jpy_ref)} |")
    curves = {}
    for k, prep in preps.items():
        md.append(f"\n## {NAMES[k]}\n")
        md.append("### 間隔だけを変えた比較 (それ以外は推奨設定)\n")
        md.append(HEAD)
        for sm, vals in (("fix", FIX), ("atr", ATR_K)):
            for v in vals:
                c = dict(REC, smode=sm, step=v)
                md.append(row(label(c), run(prep, c)[2]))
        x = g[g.data == k].assign(both=lambda d: (d.h1 > 0) & (d.h2 > 0))
        x = x.assign(sk=[label(dict(smode=s, step=v)) for s, v in zip(x.smode, x.step)])
        r = x.groupby("sk", sort=False).both.mean()
        md.append("\n間隔ごとの全組み合わせのうち前半・後半とも黒字の割合: " +
                  " / ".join(f"{a_} {v * 100:.0f}%" for a_, v in r.items()) + "\n")
        md.append("### 設定別\n")
        md.append(HEAD)
        for name, cfg in (("推奨", REC), ("ご指定 50pips", USER)):
            for lab, kw in (("完成形 (最大4本, 3・4本目逆)", {}),
                            ("救済なし (4本とも順張り)", dict(hedge_from=99)),
                            ("反転決済なし", dict(rev_from=99)),
                            ("+安全装置 1サイクル -1.5万円", dict(max_loss=100.0))):
                eq, cyc, st = run(prep, cfg, **kw)
                md.append(row(f"{name}: {lab}", st))
                if not kw:
                    curves[(k, name)] = (prep[0].time.values, eq * 150)
        md.append("\n### エントリーをランダムにした場合 (推奨のお金の管理, 20 通り)\n")
        res = [run(prep, REC, seed=s)[2]["profit_jpy"] for s in range(20)]
        md.append(f"黒字 {sum(v > 0 for v in res)}/20, 中央値 {fmt(float(np.median(res)))}\n")
    (OUT / "compare_cci.md").write_text("\n".join(md), encoding="utf-8")

    fig, axes = plt.subplots(2, 1, figsize=(10, 7))
    for ax, k in zip(axes, DATASETS):
        for name, color in (("推奨", "#2a6fdb"), ("ご指定 50pips", "#d9534f")):
            t, e = curves[(k, name)]
            ax.plot(t, e / 1e4, color=color, lw=1.2, label="recommended" if name == "推奨" else "user 50pips")
        ax.axhline(0, color="#999", lw=0.8)
        ax.set_title(k + (" (CCI on M5)" if k == "gold_m1" else " (CCI on M15, reference)"))
        ax.set_ylabel("JPY (x10,000)")
        ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(OUT / "equity_cci.png", dpi=110)
    print((OUT / "compare_cci.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
