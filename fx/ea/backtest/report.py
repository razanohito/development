"""探索結果の絞り込みとレポート作成 (optimize.py の後に実行)。

1. 前半・後半の両方で「最大DD20%以内かつ黒字」を満たした設定を候補にする
2. 開始日を 1 週間ずつずらした 8 週間 x 16 期間で耐久テスト (開始タイミング依存の確認)
3. パラメータを 1 段階ずらした近傍の設定も全期間で条件を満たすか (過剰最適化の確認)
4. 上位候補をコスト悪化 (スプレッド・スワップ) でストレステスト
5. results/results.md と equity.png を出力
"""
from __future__ import annotations

import argparse
import os

import numpy as np
import pandas as pd

from nanpin_bt import Backtester, Market, Params, FILTERS, DIR_NAMES, load_m1
from optimize import MULT, COOLDOWN, MAX_DD, WARMUP_END, SPLIT, END, label

KEYS = ["direction", "init_lot", "grid_step", "max_pos", "tp_move", "stop_pct", "filter_id"]
NUM = ["init_lot", "grid_step", "max_pos", "tp_move", "stop_pct"]
WINDOW_WEEKS = 8


def to_grid(df):
    n = len(df)
    return np.column_stack([df.direction, df.init_lot, np.full(n, MULT), df.grid_step, df.max_pos,
                            df.tp_move, df.stop_pct, np.full(n, COOLDOWN), df.filter_id]).astype(float)


def to_params(row):
    return Params(direction=int(row.direction), init_lot=float(row.init_lot), multiplier=MULT,
                  grid_step=float(row.grid_step), max_pos=int(row.max_pos), tp_move=float(row.tp_move),
                  stop_pct=float(row.stop_pct), cooldown_min=COOLDOWN, filter_id=int(row.filter_id))


def neighborhood(r):
    vals = {k: sorted(r[k].unique()) for k in NUM}
    cols = []
    for k in NUM:
        r[k + "_i"] = r[k].map({v: i for i, v in enumerate(vals[k])})
        cols.append(k + "_i")
    cols += ["direction", "filter_id"]
    arr = r[cols].to_numpy()
    idx = {tuple(row): i for i, row in enumerate(arr)}
    fdd = r.full_maxdd_pct.to_numpy()
    fok = r.full_ok.to_numpy()
    worst = np.empty(len(r))
    frac = np.empty(len(r))
    for i, row in enumerate(arr):
        w, o = [], []
        for j in range(len(NUM)):
            for d in (-1, 1):
                rr = row.copy()
                rr[j] += d
                t = idx.get(tuple(rr))
                if t is not None:
                    w.append(fdd[t])
                    o.append(fok[t])
        worst[i] = max(w)
        frac[i] = np.mean(o)
    r["nb_worst_dd"] = worst
    r["nb_ok_frac"] = frac
    return r.drop(columns=[k + "_i" for k in NUM])


def rolling(bt, cand):
    starts = pd.date_range(WARMUP_END, pd.Timestamp(END) - pd.Timedelta(weeks=WINDOW_WEEKS),
                           freq="7D", tz="UTC")
    grid = to_grid(cand)
    R, D = [], []
    for s in starts:
        x = bt.run_grid(grid, s, s + pd.Timedelta(weeks=WINDOW_WEEKS))
        R.append(x.return_pct.values)
        D.append(x.maxdd_pct.values)
    R, D = np.array(R).T, np.array(D).T
    cand = cand.copy()
    cand["w_n"] = len(starts)
    cand["w_pass"] = ((D <= MAX_DD) & (R > 0)).sum(1)
    cand["w_worst_dd"] = D.max(1)
    cand["w_min_ret"] = R.min(1)
    cand["w_med_ret"] = np.median(R, 1)
    return cand, len(starts)


def fmt_pct(x):
    return f"{x:+.1f}%"


def jpy(x):
    return f"{x/10000:+,.0f}万円"


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--data", default=os.path.join(here, "..", "data", "XAUUSD_1m_2026-03-26_2026-09-25.csv.gz"))
    ap.add_argument("--out", default=os.path.join(here, "..", "results"))
    args = ap.parse_args()

    r = pd.read_csv(os.path.join(args.out, "all_results.csv.gz"))
    df = load_m1(args.data)
    bt = Backtester(df, Market())

    r = neighborhood(r)
    cand = r[r.is_ok & r.oos_ok].reset_index(drop=True)
    cand, n_win = rolling(bt, cand)
    cand.to_csv(os.path.join(args.out, "candidates.csv.gz"), index=False, float_format="%.4f")

    # 推奨の考え方:
    #   * 全 16 期間 (少なくとも 15) で DD20%以内・黒字、どの期間でも DD20% 以内
    #   * 近傍の 75% 以上も全期間で条件を満たす (パラメータを少しずらしても壊れにくい)
    #   * 相場の向きに賭けない「両方向 + トレンドフィルタ」を優先
    strong = cand[(cand.w_pass >= n_win - 1) & (cand.w_worst_dd <= MAX_DD) & (cand.nb_ok_frac >= 0.75)]
    strong = strong.sort_values("full_return_pct", ascending=False)
    strong = strong.drop_duplicates(["full_return_pct", "full_maxdd_pct", "w_med_ret"]).reset_index(drop=True)
    # コスト悪化 (スプレッド・スワップ) でも全期間で合格すること
    stress_m = Market(spread=0.50, swap_long=-80.0, swap_short=-30.0)
    bt_s = Backtester(df, stress_m)
    sres = bt_s.run_grid(to_grid(strong), WARMUP_END, END)
    strong["stress_ok"] = ((sres.maxdd_pct <= MAX_DD) & (sres.profit_jpy > 0) & (sres.n_stopout == 0)).values
    strong = strong[strong.stress_ok]

    picks = []
    def add(df_, tag, n=1):
        for _, row in df_.head(n).iterrows():
            picks.append((tag, row))

    add(strong[(strong.direction == 2) & (strong.filter_id > 0)], "候補A: 両方向")
    add(strong[strong.direction == 1], "候補B: 売りのみ")
    add(strong[strong.direction == 0], "候補C: 買いのみ")
    # 全期間合格の中で最も利益が大きいもの (近傍は脆い)
    agg = cand[(cand.w_pass == n_win) & (cand.w_worst_dd <= MAX_DD)].sort_values("w_med_ret", ascending=False)
    add(agg, "候補D: 利益重視 (近傍は脆い)")

    # 前回「生存」した設定 (買いのみ・幅$5・最大20本) の比較
    base = r[(r.direction == 0) & (r.init_lot == 0.01) & (r.grid_step == 5) & (r.max_pos == 20)
             & (r.tp_move == 10) & (r.stop_pct == 0) & (r.filter_id == 0)].iloc[0]

    # ストレステスト
    rows = []
    curves = {}
    for tag, row in picks + [("前回の生存設定", base)]:
        p = to_params(row)
        full = bt.run(p, WARMUP_END, END, curve=True)
        st = bt_s.run(p, WARMUP_END, END)
        curves[tag] = full.pop("curve")
        rows.append((tag, row, full, st))

    # ナイーブな最適化 (前半スコア 1 位を採用) の結果
    naive = r[r.is_ok].sort_values("is_score", ascending=False).drop_duplicates(
        ["is_return_pct", "is_maxdd_pct", "oos_return_pct"]).head(10)

    by_dir = r.groupby("direction")[["is_ok", "oos_ok", "full_ok"]].mean() * 100
    by_filt = r.groupby("filter_id")[["is_ok", "oos_ok", "full_ok"]].mean() * 100
    by_stop = r.groupby("stop_pct")[["is_ok", "oos_ok", "full_ok"]].mean() * 100

    L = []
    w = L.append
    w("# 金ナンピンEA 探索結果\n")
    w(f"* データ: XAUUSD 1分足 (getdata-finance 無料サンプル, MIT) {df.datetime.iloc[0]:%Y-%m-%d} 〜 {df.datetime.iloc[-1]:%Y-%m-%d}")
    w(f"* 取引期間: {WARMUP_END} 〜 {END} (それ以前はトレンドフィルタのウォームアップ)")
    w(f"  * 前半 (最適化): {WARMUP_END} 〜 {SPLIT}　金 約4,830→4,080ドルの下落局面")
    w(f"  * 後半 (検証): {SPLIT} 〜 {END}　金 約4,000→4,620→4,280ドルの上昇・反落局面")
    m = bt.m
    w(f"* 口座: {m.balance_jpy/1e4:,.0f}万円 (USDJPY {m.usdjpy:g} 固定), レバレッジ {m.leverage:g}倍, 維持率 {m.stopout_level*100:.0f}% でロスカット")
    w(f"* コスト: スプレッド ${m.spread:.2f}, スワップ 買い ${m.swap_long:g} / 売り ${m.swap_short:g} (1lot・1日, 水曜3倍)")
    w(f"* 倍率 {MULT} 固定。合格条件 = 最大DD {MAX_DD:.0f}%以内 かつ 黒字 かつ ロスカットなし\n")

    w(f"## 結論\n")
    w(f"* 探索 {len(r):,} 通りのうち、前半・後半の両方で合格したのは {len(cand):,} 通り ({len(cand)/len(r)*100:.1f}%)。")
    w(f"* **前半だけで最適化すると後半はほぼ通用しない。** 前半スコア上位10件のうち後半も合格は {int(naive.oos_ok.sum())} 件。"
      "前半が下落相場だったため「売りのみ・大きめロット」が上位を占め、後半の上昇で崩れた。")
    w(f"* 開始日を1週ずつずらした {n_win} 期間 (各{WINDOW_WEEKS}週) のほぼ全てで合格し、パラメータを1段階ずらしても崩れにくく、"
      f"コストを悪化させても合格する設定は {len(strong)} 通り。利益は全期間 (約5か月) で +10〜30% 程度。")
    w("* 前回の「生存」設定 (買いのみ・幅$5・最大20本) は今回のデータではロスカットされた。")
    w("* 6か月分しか無料の1分足が取得できなかったため、全候補は同じデータで選んでいる。"
      "**本番前に必ずデモ口座でのフォワードテストと、MT5 ストラテジーテスターでの長期検証を行うこと。**\n")

    w("## 候補設定\n")
    w(f"| 区分 | 方向 | 初期ロット | ナンピン幅 | 最大本数 | 利確 | 口座損切り | フィルタ | 前半 利益/DD | 後半 利益/DD | 全期間 利益 | 全期間 DD | {n_win}期間合格 | 期間最悪DD | 近傍合格率 | コスト悪化時 利益/DD |")
    w("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for tag, row, full, st in rows:
        tp_jpy = row.tp_move * row.init_lot * 100 * m.usdjpy
        w(f"| {tag} | {DIR_NAMES[int(row.direction)]} | {row.init_lot:.2f} | ${row.grid_step:g} | {int(row.max_pos)} | "
          f"¥{tp_jpy:,.0f} (${row.tp_move:g}幅相当) | {'なし' if row.stop_pct == 0 else f'{row.stop_pct*100:.0f}%'} | "
          f"{FILTERS[int(row.filter_id)][0]} | {fmt_pct(row.is_return_pct)} / {row.is_maxdd_pct:.1f}% | "
          f"{fmt_pct(row.oos_return_pct)} / {row.oos_maxdd_pct:.1f}% | {jpy(full['profit_jpy'])} ({fmt_pct(full['return_pct'])}) | "
          f"{full['maxdd_pct']:.1f}% | {int(row.get('w_pass', np.nan)) if not pd.isna(row.get('w_pass', np.nan)) else '-'}/{n_win} | "
          f"{'-' if pd.isna(row.get('w_worst_dd', np.nan)) else format(row.w_worst_dd, '.1f') + '%'} | {row.nb_ok_frac*100:.0f}% | "
          f"{fmt_pct(st['return_pct'])} / {st['maxdd_pct']:.1f}% |")
    w("")
    w("* 利確: バスケットの合計損益 (スワップ込み) がこの金額に達したら一括決済。初期ロットが指定ドル幅動いた利益に相当。")
    w("* 近傍合格率: 初期ロット・幅・本数・利確・損切りのどれか1つを1段階ずらした設定のうち、全期間で合格した割合。")
    w("* コスト悪化: スプレッド $0.50, スワップ 買い -$80 / 売り -$30。")
    w("* 前回の生存設定は候補ではなく比較用 (期間テスト対象外)。\n")

    w("## ナイーブ最適化 (前半スコア上位) の後半成績\n")
    w("| 設定 | 前半 利益/DD | 後半 利益/DD | 後半合格 |")
    w("|---|---|---|---|")
    for _, row in naive.iterrows():
        w(f"| {label(row)} | {fmt_pct(row.is_return_pct)} / {row.is_maxdd_pct:.1f}% | "
          f"{fmt_pct(row.oos_return_pct)} / {row.oos_maxdd_pct:.1f}% | {'○' if row.oos_ok else '×'} |")
    w("")

    w("## 要素別の合格率 (%)\n")
    for title, g, names in (("売買方向", by_dir, DIR_NAMES), ("トレンドフィルタ", by_filt, {i: f[0] for i, f in enumerate(FILTERS)}),
                            ("口座損切り", by_stop, None)):
        w(f"**{title}**\n")
        w("| 値 | 前半 | 後半 | 全期間 |")
        w("|---|---|---|---|")
        for k, v in g.iterrows():
            name = names[k] if names else ("なし" if k == 0 else f"{k*100:.0f}%")
            w(f"| {name} | {v.is_ok:.0f} | {v.oos_ok:.0f} | {v.full_ok:.0f} |")
        w("")
    w("H4 SMA100 フィルタが最も安定。方向を固定する設定は相場の向きが変わると崩れる。\n")
    w("![エクイティ曲線](equity.png)\n")
    with open(os.path.join(args.out, "results.md"), "w") as f:
        f.write("\n".join(L))

    plot(df, curves, os.path.join(args.out, "equity.png"))
    print("\n".join(L))


def plot(df, curves, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    for f in ("IPAGothic", "IPAexGothic", "Noto Sans CJK JP"):
        if any(f in x.name for x in font_manager.fontManager.ttflist):
            plt.rcParams["font.family"] = f
            break
    colors = ["#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#e34948"]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, gridspec_kw={"height_ratios": [1, 2]})
    s = df.set_index("datetime")["close"]
    s = s[s.index >= pd.Timestamp(WARMUP_END, tz="UTC")].resample("1h").last().dropna()
    a1.plot(s.index, s.values, color="#52514e", lw=1.2)
    a1.set_ylabel("XAUUSD ($)")
    a1.axvline(pd.Timestamp(SPLIT, tz="UTC"), color="#9a9893", lw=1, ls="--")
    a1.text(pd.Timestamp(SPLIT, tz="UTC"), a1.get_ylim()[1], " 後半(検証)", va="top", fontsize=9, color="#52514e")
    for (tag, cv), col in zip(curves.items(), colors):
        cv = cv.copy()
        cv.index = pd.to_datetime(cv.index, utc=True)
        cv = cv.resample("1h").last().dropna() / 1e4
        a2.plot(cv.index, cv.values, color=col, lw=2 if not tag.startswith("前回") else 1.2, label=tag)
    a2.axhline(500, color="#9a9893", lw=0.8)
    a2.axhline(400, color="#e34948", lw=0.8, ls=":")
    a2.text(a2.get_xlim()[0], 400, " DD20%ライン(元手比)", va="bottom", fontsize=8, color="#52514e")
    a2.axvline(pd.Timestamp(SPLIT, tz="UTC"), color="#9a9893", lw=1, ls="--")
    a2.set_ylabel("有効証拠金 (万円)")
    a2.set_ylim(bottom=max(0, a2.get_ylim()[0]))
    a2.legend(loc="upper left", fontsize=9, frameon=False)
    for ax in (a1, a2):
        ax.grid(axis="y", color="#e6e5e0", lw=0.6)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    fig.tight_layout()
    fig.savefig(path, dpi=110)


if __name__ == "__main__":
    main()
