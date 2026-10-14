"""EURUSD / GBPUSD でナンピンEAの設定を総当たりし、前半で最適化・後半で検証する。

    python3 fetch_data.py      # 先にデータ取得
    python3 optimize.py        # results/ に CSV・グラフ・summary.md を出力

採用基準 (金の検証と同じ): 前半期間で「破綻なし・最大DD 20%以内・黒字」の中から利益最大の設定を選び、
その設定をそのまま後半期間に当てて成績を見る。
"""
from __future__ import annotations

import itertools
import pathlib
import time

import numpy as np
import pandas as pd

import nanpin_bt as bt

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "results"

PAIRS = {
    # spread は海外MT5口座のスタンダード程度を想定した保守的な値
    "EURUSD": dict(spread_pips=1.0),
    "GBPUSD": dict(spread_pips=1.5),
}

GRID = dict(
    mode=[bt.MODE_BUY, bt.MODE_SELL, bt.MODE_BOTH],
    init_lot=[0.01, 0.02, 0.05, 0.10],
    mult=[1.0, 1.2, 1.5],
    grid_pips=[10, 20, 30, 50, 80],
    max_pos=[5, 10, 15, 20],
    tp_pips=[10, 20, 40],
    stop_pct=[0, 10, 20, 30],
    use_ma=[0, 1],
)
DD_LIMIT = 20.0
SAFE_DD = 5.0
TOP_N = 10

LABELS = {
    "mode": "方向", "init_lot": "初期ロット", "mult": "倍率", "grid_pips": "ナンピン幅(pips)",
    "max_pos": "最大本数", "tp_pips": "利確幅(pips)", "stop_pct": "口座損切り(%)", "use_ma": "200MAフィルタ",
}


def param_matrix() -> np.ndarray:
    keys = list(GRID)
    rows = list(itertools.product(*(GRID[k] for k in keys)))
    return np.array(rows, dtype=np.float64)


def describe(row) -> str:
    r = {k: row[k] for k in bt.PARAM_COLS}
    stop = f"口座損切り{int(r['stop_pct'])}%" if r["stop_pct"] > 0 else "損切りなし"
    ma = "200MAあり" if r["use_ma"] else "MAなし"
    return (f"{bt.MODE_NAMES[int(r['mode'])]} / 初期{r['init_lot']:.2f}ロット / 倍率{r['mult']:g} / "
            f"幅{r['grid_pips']:g}pips / 最大{int(r['max_pos'])}本 / 利確{r['tp_pips']:g}pips / {stop} / {ma}")


def fmt_yen(v: float) -> str:
    return f"{v:+,.0f}円"


def ok_mask(df: pd.DataFrame) -> pd.Series:
    return (~df.busted) & (df.max_dd_pct <= DD_LIMIT) & (df.profit_jpy > 0)


def tendency(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """パラメータ値ごとの破綻率・DD超過率・合格率。"""
    rows = []
    for key in GRID:
        for val in GRID[key]:
            r = {"param": LABELS[key], "value": bt.MODE_NAMES[val] if key == "mode" else
                 ("あり" if val else "なし") if key == "use_ma" else val}
            for name, df in frames.items():
                sub = df[df[key] == val]
                r[f"{name}_破綻率%"] = round(sub.busted.mean() * 100, 1)
                r[f"{name}_DD20%超%"] = round((sub.max_dd_pct > DD_LIMIT).mean() * 100, 1)
                r[f"{name}_合格率%"] = round(ok_mask(sub).mean() * 100, 1)
            rows.append(r)
    return pd.DataFrame(rows)


def daily_arrays(pair: str):
    """2022-03 以降の日足終値 (H1 データの後) を疑似バーにする。O=H=L=C なので日中の振れは無視される。"""
    d = pd.read_csv(DATA / f"{pair}_D1_close.csv", parse_dates=["time"])
    d = d[d.time >= "2022-03-07"].reset_index(drop=True)
    df = pd.DataFrame({"time": d.time, "open": d.close, "high": d.close, "low": d.close, "close": d.close})
    # H1 の 200 本 ≒ 8.3 営業日なので日足では 8 日移動平均で代用
    return df, bt.prepare(df, ma_period=8)


def md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def plot_curves(curves: dict, path: pathlib.Path) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError:
        return
    for f in ("IPAGothic", "IPAexGothic", "Noto Sans CJK JP"):
        if any(f == x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    fig, axes = plt.subplots(len(curves), 1, figsize=(10, 3.4 * len(curves)), squeeze=False)
    for ax, (pair, (times, eq, split_time)) in zip(axes[:, 0], curves.items()):
        ax.plot(times, eq / 1e4, lw=1)
        ax.axvline(split_time, color="gray", ls="--", lw=1)
        ax.axhline(500, color="black", lw=0.6)
        ax.set_title(f"{pair} 採用設定の有効証拠金 (万円)  点線より右が検証期間")
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    P = param_matrix()
    print(f"{len(P)} 通り x {len(PAIRS)} 通貨")
    md = []
    best_rows = []
    curves = {}
    tend_all = {}
    for pair, cfg in PAIRS.items():
        acct = bt.Account(**cfg)
        df = bt.load_h1(DATA / f"{pair}_H1.csv")
        arr = bt.prepare(df)
        n = len(df)
        mid = n // 2
        t0, tm, t1 = df.time.iloc[0], df.time.iloc[mid], df.time.iloc[-1]
        tic = time.time()
        is_ = bt.run_grid(arr, P, acct, 0, mid)
        oos = bt.run_grid(arr, P, acct, mid, n)
        full = bt.run_grid(arr, P, acct, 0, n)
        print(f"{pair}: {time.time() - tic:.1f}s")
        is_.to_csv(OUT / f"{pair}_in_sample_all.csv.gz", index=False)
        oos.to_csv(OUT / f"{pair}_out_of_sample_all.csv.gz", index=False)

        # 前半で合格した設定を利益順に並べ、後半成績を横に付ける
        # 同じ成績が並んだ場合 (損切りが一度も発動しない等) は、事前に決めたルールで
        # 「DDが小さい → 口座損切りが浅い(なしは最後) → MAフィルタあり」を優先する
        cand = is_[ok_mask(is_)].assign(_stop=lambda d: d.stop_pct.where(d.stop_pct > 0, 999))
        cand = cand.sort_values(["profit_jpy", "max_dd_pct", "_stop", "use_ma"],
                                ascending=[False, True, True, False]).drop(columns="_stop")
        pair_oos = oos.loc[cand.index]
        top = pd.DataFrame({
            "設定": [describe(r) for _, r in cand.head(TOP_N).iterrows()],
            "前半利益": [fmt_yen(v) for v in cand.head(TOP_N).profit_jpy],
            "前半DD%": cand.head(TOP_N).max_dd_pct.values,
            "後半利益": [fmt_yen(v) for v in pair_oos.head(TOP_N).profit_jpy],
            "後半DD%": pair_oos.head(TOP_N).max_dd_pct.values,
            "後半破綻": ["破綻" if b else "-" for b in pair_oos.head(TOP_N).busted],
        })
        top.to_csv(OUT / f"{pair}_top{TOP_N}.csv", index=False)

        n_ok_is = int(ok_mask(is_).sum())
        n_ok_both = int((ok_mask(is_) & ok_mask(oos)).sum())
        stats = dict(
            pair=pair, total=len(P),
            is_bust=int(is_.busted.sum()), oos_bust=int(oos.busted.sum()), full_bust=int(full.busted.sum()),
            is_ok=n_ok_is, both_ok=n_ok_both,
            oos_ok_given_is=round(n_ok_both / n_ok_is * 100, 1) if n_ok_is else 0.0,
        )

        # 採用ルール A: 金と同じ「前半 DD20%以内で利益最大」
        # 採用ルール B: 余裕を見て「前半 DD5%以内で利益最大」(後半でDDが2〜3倍に膨らむ傾向への備え)
        picks = [("A: 前半DD20%以内で利益最大", cand)]
        cand_b = cand[cand.max_dd_pct <= SAFE_DD]
        picks.append((f"B: 前半DD{SAFE_DD:g}%以内で利益最大", cand_b))
        m15 = bt.load_h1(DATA / f"{pair}_M15.csv")
        m_arr = bt.prepare(m15, ma_period=800)  # 15分足 800 本 = 1時間足 200 本相当
        m_mid = int(m15.time.searchsorted(tm))
        dd, darr = daily_arrays(pair)
        stats["recent_start"] = str(dd.time.iloc[0].date())
        stats["recent_end"] = str(dd.time.iloc[-1].date())
        for rule, cset in picks:
            if not len(cset):
                continue
            b = cset.iloc[0]
            p = bt.Params(*[b[c] for c in bt.PARAM_COLS])
            p.mode, p.max_pos, p.use_ma = int(p.mode), int(p.max_pos), int(p.use_ma)
            r_is = is_.loc[b.name]
            r_oos = oos.loc[b.name]
            r_full, eq = bt.run_one(arr, p, acct, 0, n, curve=True)
            curves[f"{pair} ルール{rule[0]}"] = (df.time.values, eq, tm)
            # 2022-03 以降の日足終値での簡易確認 (最適化にも検証にも使っていない期間)
            r_recent = bt.run_one(darr, p, acct, 0, len(dd))
            # 海外口座 (レバ500倍・維持率20%ロスカット) でも同じか
            r_ovs = bt.run_one(arr, p, bt.Account(leverage=500, stopout_level=0.2, **cfg), mid, n)
            # 15分足で後半を再計算 (1時間足のバー内経路の仮定の影響を確認)
            r_m15 = bt.run_one(m_arr, p, acct, m_mid, len(m15))
            best_rows.append(dict(
                通貨=pair, ルール=rule, 設定=describe(b),
                前半利益=fmt_yen(r_is.profit_jpy), 前半DD=f"{r_is.max_dd_pct:.1f}%",
                後半利益=fmt_yen(r_oos.profit_jpy), 後半DD=f"{r_oos.max_dd_pct:.1f}%",
                後半破綻="破綻" if r_oos.busted else "なし",
                後半合格="合格" if (not r_oos.busted and r_oos.max_dd_pct <= DD_LIMIT and r_oos.profit_jpy > 0) else "不合格",
                全期間利益=fmt_yen(r_full.profit_jpy), 全期間DD=f"{r_full.max_dd_pct:.1f}%",
                日足2022_2026利益=fmt_yen(r_recent.profit_jpy), 日足2022_2026DD=f"{r_recent.max_dd_pct:.1f}%",
                後半_15分足=f"{fmt_yen(r_m15.profit_jpy)} / DD {r_m15.max_dd_pct:.1f}%",
                後半_海外口座DD=f"{r_ovs.max_dd_pct:.1f}%",
                後半決済回数=int(r_oos.baskets), 後半最大保有本数=int(r_oos.max_open),
            ))

        # 後半で最も良かった設定 (参考: 後から見た最良であり、事前には選べない)
        hind = oos[ok_mask(oos)].sort_values("profit_jpy", ascending=False)
        stats["oos_hindsight_best"] = describe(hind.iloc[0]) if len(hind) else "-"
        stats["oos_hindsight_profit"] = fmt_yen(hind.iloc[0].profit_jpy) if len(hind) else "-"
        stats["period"] = f"{t0:%Y-%m-%d} 〜 {tm:%Y-%m-%d} 〜 {t1:%Y-%m-%d}"
        stats["spread"] = cfg["spread_pips"]

        tend = tendency({"前半": is_, "後半": oos})
        tend.to_csv(OUT / f"{pair}_tendency.csv", index=False)
        tend_all[pair] = tend

        md.append(f"## {pair}\n")
        md.append(f"- 期間: {stats['period']} (前半=最適化 / 後半=検証)、スプレッド {cfg['spread_pips']} pips")
        md.append(f"- {len(P):,} 通り中 破綻(ロスカット): 前半 {stats['is_bust']:,} / 後半 {stats['oos_bust']:,} / "
                  f"全期間 {stats['full_bust']:,}")
        md.append(f"- 前半合格(破綻なし・DD20%以内・黒字): {n_ok_is:,} 通り → そのうち後半も合格: {n_ok_both:,} 通り "
                  f"({stats['oos_ok_given_is']}%)")
        md.append(f"- 参考・後半だけ見た最良(後知恵): {stats['oos_hindsight_best']} {stats['oos_hindsight_profit']}\n")
        md.append(f"### 前半の上位{TOP_N}設定とその後半成績\n")
        md.append(md_table(top) + "\n")
        md.append("### パラメータ別の破綻率・DD20%超率・合格率\n")
        md.append(md_table(tend) + "\n")

    best = pd.DataFrame(best_rows)
    best.to_csv(OUT / "best_settings.csv", index=False)
    head = ["# EURUSD / GBPUSD ナンピンEA 設定探索結果\n",
            f"{len(P):,} 通り/通貨。前提: 元手500万円・USDJPY 150 固定換算・国内口座想定(レバ25倍・維持率100%でロスカット)・"
            "スワップ 1ロット1日 -5 USD。\n",
            "## 通貨ごとの採用設定(前半で選び、後半で検証)\n",
            "日足2022_2026 は O=H=L=C の日足終値だけで回した簡易確認 (日中の振れを無視するので DD は過小評価)。\n",
            md_table(best.assign(列=best.通貨 + " " + best.ルール.str[0]).set_index("列")
                     .drop(columns=["通貨"]).T.reset_index().rename(columns={"index": "項目"})) + "\n"]
    (OUT / "summary.md").write_text("\n".join(head + md), encoding="utf-8")
    plot_curves(curves, OUT / "equity_best.png")
    print((OUT / "summary.md").read_text(encoding="utf-8")[:3000])


if __name__ == "__main__":
    main()
