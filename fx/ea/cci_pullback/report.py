"""study.py の結果から results/results.md・equity.png・推奨設定の取引一覧を作る。"""
from __future__ import annotations

import dataclasses
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import cci_bt  # noqa: E402
from cci_bt import (CAPITAL_JPY, SYMBOLS, TP_LABEL, TP_MODES, Params, build_frame,  # noqa: E402
                    entry_signal, metrics, run)
from data import SPLIT, bars  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "results"

# 推奨設定 (EA の既定値と同じ)
REC = dict(symbol="XAUUSD", ltf="M15", htf="H1", env="structure", env_cci=50.0, candle=0,
           ema_touch=0, fib=0, max_depth=0.0, hard_sl=1, tp_mode="diverge")
# 画像のルールをそのまま (追加フィルタなし)
BASE = dict(env="structure", env_cci=50.0, candle=0, ema_touch=0, fib=0, max_depth=0.0, hard_sl=1)
FILTER_KEYS = ["env", "env_cci", "candle", "ema_touch", "fib", "max_depth", "hard_sl"]
N_RANDOM = 300


def yen(x: float) -> str:
    return f"{x:+,.0f}円"


def pf(x: float) -> str:
    return "∞" if np.isinf(x) else ("-" if np.isnan(x) else f"{x:.2f}")


def params_of(row: dict) -> Params:
    kw = {k: row[k] for k in ["ltf", "htf", *FILTER_KEYS, "tp_mode"]}
    return Params(**kw)


def frame(sym: str, ltf: str, htf: str) -> pd.DataFrame:
    return build_frame(bars(sym, ltf), bars(sym, htf), Params(ltf=ltf, htf=htf))


def random_baseline(d: pd.DataFrame, p: Params, sym: str, n: int = N_RANDOM, seed: int = 0):
    """同じ環境認識が成立している足からランダムに同じ回数エントリーし、同じ決済ルールで比べる。"""
    sig, slp = entry_signal(d, p)
    env = d["h_up"].fillna(False).to_numpy(bool) & (d["h_cci"].to_numpy() >= p.env_cci)
    env &= ~np.isnan(d["atr"].to_numpy())
    idx = np.flatnonzero(env)
    k = int(sig.sum())
    rng = np.random.default_rng(seed)
    orig = cci_bt.entry_signal
    nets = []
    try:
        for _ in range(n):
            s = np.zeros(len(d), bool)
            s[rng.choice(idx, k, replace=False)] = True
            cci_bt.entry_signal = lambda _d, _p, s=s: (s, slp)
            nets.append(metrics(cci_bt.run(d, p, SYMBOLS[sym]))["net_jpy"])
    finally:
        cci_bt.entry_signal = orig
    return np.array(nets)


def main() -> None:
    df = pd.read_csv(OUT / "grid.csv.gz")
    df["combo"] = df.symbol + " " + df.ltf + "/" + df.htf
    combos = list(dict.fromkeys(df.combo))
    md: list[str] = []
    w = md.append

    # ------------------------------------------------ 推奨設定
    sym = REC["symbol"]
    p = params_of(REC)
    d = frame(sym, p.ltf, p.htf)
    tr = run(d, p, SYMBOLS[sym])
    tr.to_csv(OUT / "recommended_trades.csv", index=False, float_format="%.5g")
    split = SPLIT[sym]
    m_all = metrics(tr)
    m_a = metrics(tr[tr.entry_time < split])
    m_b = metrics(tr[tr.entry_time >= split])
    rnd = random_baseline(d, p, sym)
    p_rand = float((rnd >= m_all["net_jpy"]).mean())

    # 利確方法の比較 (推奨と同じ足・フィルタ)
    tp_rows = []
    trades_by_tp = {}
    for tp in TP_MODES:
        q = dataclasses.replace(p, tp_mode=tp)
        t = run(d, q, SYMBOLS[sym])
        trades_by_tp[tp] = t
        tp_rows.append((tp, metrics(t), metrics(t[t.entry_time < split]), metrics(t[t.entry_time >= split]),
                        float((random_baseline(d, q, sym, n=100) >= metrics(t)["net_jpy"]).mean())))

    # 近傍 (1項目だけ変えた設定)
    neigh = []
    for k, vals in [("env_cci", [-1e9, 0.0, 100.0]), ("env", ["ema", "none"]), ("candle", [1]),
                    ("ema_touch", [1]), ("fib", [1]), ("max_depth", [200.0]), ("hard_sl", [0])]:
        for v in vals:
            r = df[(df.symbol == sym) & (df.ltf == p.ltf) & (df.htf == p.htf) & (df.tp_mode == p.tp_mode)]
            for kk in FILTER_KEYS:
                r = r[r[kk] == (v if kk == k else REC[kk])]
            if len(r):
                neigh.append((k, v, r.iloc[0]))
    tf_rows = []
    for ltf, htf in [("M5", "H1"), ("M5", "H4"), ("M15", "H4")]:
        r = df[(df.symbol == sym) & (df.ltf == ltf) & (df.htf == htf) & (df.tp_mode == p.tp_mode)]
        for kk in FILTER_KEYS:
            r = r[r[kk] == REC[kk]]
        tf_rows.append((f"{ltf}/{htf}", r.iloc[0]))

    # 利確方法の総合順位 (同じ足・同じフィルタの中で PF を順位付け、30回以上の取引のみ)
    fk = ["combo", *FILTER_KEYS]
    g = df[df.all_trades >= 30].copy()
    g["rk"] = g.groupby(fk).all_pf.rank(ascending=False)
    rank = (g.groupby("tp_mode").agg(rk=("rk", "mean"), pfm=("all_pf", "median"))
              .sort_values("rk"))
    rank_sym = g.groupby(["tp_mode", "symbol"]).rk.mean().unstack()

    # 画像のルールそのまま (BASE) の全組み合わせ
    base = df.copy()
    for k, v in BASE.items():
        base = base[base[k] == v]

    # 前半・後半ともに黒字の割合
    surv = df.assign(ok=(df.a_pf > 1) & (df.b_pf > 1) & (df.a_trades >= 20) & (df.b_trades >= 20))
    surv_tab = surv.groupby("combo").agg(n=("ok", "size"), ok=("ok", "sum"), pf=("all_pf", "median"))

    # ------------------------------------------------ results.md
    w("# CCI 押し目買い (風林火山) EA 検証結果\n")
    w("* 金 (XAUUSD): 1分足 2026-03-26〜2026-09-25 (getdata-finance 無料サンプル) を 5分/15分/1時間/4時間足に変換。"
      "前半 = 〜2026-07-06 (下落局面), 後半 = 2026-07-06〜 (上昇・反落局面)")
    w("* EURUSD / GBPUSD: 15分足・1時間足 2012-11〜2022-03 (ejtraderLabs)。前半 = 〜2017年, 後半 = 2018年〜")
    w("* ロット 0.01 固定・買いのみ・同時に1ポジション。元手 500万円, USDJPY 150 固定")
    w("* コスト: 金 スプレッド $0.30・買いスワップ -$50/lot/日、EURUSD 0.5pips・GBPUSD 0.7pips・買いスワップ -$5/lot/日")
    w("* シグナルは確定足で判定し、次の足の始値で売買 (未来の足は見ない)。緊急損切の逆指値だけ足の途中で約定\n")

    w("## 結論\n")
    w(f"* **画像のルールで勝てたのは「金・15分足エントリー・1時間足で環境認識」だけ。** "
      f"約6か月で {m_all['trades']} 回、勝率 {m_all['win_rate']:.0f}%、PF {pf(m_all['pf'])}、"
      f"損益 {yen(m_all['net_jpy'])} (0.01ロット)、最大DD {m_all['max_dd_jpy']:,.0f}円。前半・後半とも黒字 "
      f"(前半 PF {pf(m_a['pf'])} / 後半 PF {pf(m_b['pf'])})。")
    w(f"* 同じ環境 (上位足が上昇トレンド・CCI≥50) の中でランダムにエントリーして同じ決済ルールを使うと、"
      f"{N_RANDOM}回中この成績以上は {p_rand * 100:.0f}% だけ。CCI のトリガーにはある程度の意味がありそう。"
      f"ただし取引回数が少なく、金が上昇基調だった半年分しかデータがない。")
    w("* 5分足エントリーや4時間足の環境認識に変えると損益はほぼトントンか赤字になり、時間足の選び方に敏感。")
    w(f"* **EURUSD・GBPUSD (約9年) では、どの設定でもほぼ赤字。** 画像のルールそのままだと PF 0.74〜0.90 で、"
      "コストを0にしても PF 1 を下回った (このルール自体に優位性がない)。")
    w("* **利確「勢いがなくなりレンジになりそうな時」は、早く逃げる定義ほど成績が悪かった。** "
      "CCI のピークアウトや +100 割れで降りると利益が伸びず、"
      "「高値更新したのに CCI が前のピークを超えない (ダイバージェンス)、または CCI が 0 割れ」で降りる定義が総合1位。"
      "EA の既定値はこれにした。")
    w("* 0.01 ロットだと元手 500万円に対して損益は ±数万円 (±1%未満) の規模。\n")

    w("## ルールの数値化\n")
    w("| 画像のルール | EA・検証での定義 |")
    w("|---|---|")
    w("| ① 上位足で上昇トレンド (高値更新・押し安値維持) | 上位足の確定足で、直近2つのスイング高値・スイング安値 (左右2本のフラクタル) がどちらも切り上がり、終値が直近の押し安値より上 |")
    w("| ① 上位足 CCI が +100 付近で推移 | 上位足 CCI(14) ≥ 50 (0 / 100 / 条件なし も比較) |")
    w("| ② 下位足で CCI が -100〜-150 まで下落後、-100 を上抜け | 下位足 CCI(14) が前の足で -100 以下、確定足で -100 超え。深さの上限 (-200) は任意 |")
    w("| ② 陽線包み足 or 下ヒゲ反発 (任意) | 確定足か1本前が陽線包み足、または下ヒゲが足の長さの50%以上 |")
    w("| ② 20EMA 付近からの反転 (任意) | 直近10本の安値が 20EMA + 0.5ATR 以下まで来ていた |")
    w("| 補足 フィボ 50〜61.8% (任意) | 直近10本の安値が、上位足の直近押し安値→高値の 38.2〜78.6% 押し |")
    w("| ③ 損切: CCI 再び -100 割れ + 陰線確定 | 確定足で CCI < -100 かつ 終値 < 始値 → 次の足の始値で決済 |")
    w("| (画像になし) 緊急損切 | 直近10本の安値 - 0.2ATR に逆指値 (急落対策。なしも比較) |")
    w("| ③ 利確: CCI +100 以上に達した後、勢いが鈍る / ダイバージェンス | 下表の9通りを比較 |")
    w("| 補足 CCI 角度で保有期間を調整 | 利確方法「角度で切替」として比較 |\n")

    w("## 利確方法の比較\n")
    w(f"### 推奨と同じ条件 (金 {p.ltf}/{p.htf}, 追加フィルタなし)\n")
    w("| 利確方法 | 回数 | 勝率 | PF | 損益 | 最大DD | 前半 PF | 後半 PF | ランダム以上の割合 |")
    w("|---|---|---|---|---|---|---|---|---|")
    for tp, m, ma, mb, pr in tp_rows:
        w(f"| {TP_LABEL[tp]} | {m['trades']} | {m['win_rate']:.0f}% | {pf(m['pf'])} | {yen(m['net_jpy'])} | "
          f"{m['max_dd_jpy']:,.0f}円 | {pf(ma['pf'])} | {pf(mb['pf'])} | {pr * 100:.0f}% |")
    w("\n「ランダム以上の割合」は、同じ環境でランダムにエントリーした100回のうち損益がこの設定以上だった割合 (小さいほどエントリーに意味がある)。\n")

    w("### 全 23,040 通りでの総合順位\n")
    w("同じ時間足・同じフィルタの組み合わせの中で、9通りの利確方法を PF で順位付けした平均 (1 が最良。取引30回以上の組み合わせのみ)。\n")
    w("| 利確方法 | 平均順位 | 金 | EURUSD | GBPUSD | PF 中央値 |")
    w("|---|---|---|---|---|---|")
    for tp, r in rank.iterrows():
        rs = rank_sym.loc[tp]
        w(f"| {TP_LABEL[tp]} | {r.rk:.2f} | {rs.get('XAUUSD', np.nan):.2f} | {rs.get('EURUSD', np.nan):.2f} | "
          f"{rs.get('GBPUSD', np.nan):.2f} | {r.pfm:.2f} |")
    w("")

    w("## 推奨設定 (EA の既定値)\n")
    w(f"金 (XAUUSD)・{p.ltf} エントリー・{p.htf} 環境認識・スイング構造 + 上位足 CCI≥{p.env_cci:.0f}・"
      f"緊急損切あり・利確 = {TP_LABEL[p.tp_mode]}。陽線包み足・20EMA・フィボ・深さ制限は使わない。\n")
    w("| 期間 | 回数 | 勝率 | PF | 損益 | 最大DD | 平均利益 | 平均損失 | 平均保有 |")
    w("|---|---|---|---|---|---|---|---|---|")
    for name, m in (("全期間", m_all), ("前半", m_a), ("後半", m_b)):
        w(f"| {name} | {m['trades']} | {m['win_rate']:.0f}% | {pf(m['pf'])} | {yen(m['net_jpy'])} | "
          f"{m['max_dd_jpy']:,.0f}円 ({m['max_dd_jpy'] / CAPITAL_JPY * 100:.2f}%) | {yen(m['avg_win_jpy'])} | "
          f"{yen(m['avg_loss_jpy'])} | {m['avg_bars'] * 15:.0f}分 |")
    w("")
    rc = tr.groupby("reason").pnl_jpy.agg(["size", "sum"])
    w("決済理由: " + "、".join(f"{k} {int(v['size'])}回 ({yen(v['sum'])})" for k, v in rc.iterrows()) + "\n")
    mon = tr.groupby(tr.entry_time.dt.to_period("M")).pnl_jpy.agg(["size", "sum"])
    w("| 月 | 回数 | 損益 |")
    w("|---|---|---|")
    for k, v in mon.iterrows():
        w(f"| {k} | {int(v['size'])} | {yen(v['sum'])} |")
    w("")
    w("### 1項目だけ変えた場合\n")
    w("| 変更 | 回数 | PF | 損益 | 前半 PF | 後半 PF |")
    w("|---|---|---|---|---|---|")
    label = {"env_cci": "上位足CCI下限", "env": "環境認識", "candle": "ローソク足条件", "ema_touch": "20EMA条件",
             "fib": "フィボ条件", "max_depth": "CCI深さ上限", "hard_sl": "緊急損切"}
    vlabel = lambda k, v: ("条件なし" if (k == "env_cci" and v < -1e8) else
                           {"ema": "上位足 EMA50 上向き", "none": "なし", "structure": "スイング構造"}.get(v, v) if k == "env" else
                           ("あり" if v == 1 else "なし") if k in ("candle", "ema_touch", "fib", "hard_sl") else
                           ("なし" if v == 0 else f"-{v:.0f}") if k == "max_depth" else f"{v:.0f}")
    for k, v, r in neigh:
        w(f"| {label[k]} → {vlabel(k, v)} | {int(r.all_trades)} | {pf(r.all_pf)} | {yen(r.all_net_jpy)} | "
          f"{pf(r.a_pf)} | {pf(r.b_pf)} |")
    for name, r in tf_rows:
        w(f"| 時間足 → {name} | {int(r.all_trades)} | {pf(r.all_pf)} | {yen(r.all_net_jpy)} | {pf(r.a_pf)} | {pf(r.b_pf)} |")
    w("")

    w("## 画像のルールそのまま (追加フィルタなし) の全銘柄・時間足\n")
    w("PF (前半 / 後半)。1 未満は赤字。\n")
    w("| 利確方法 | " + " | ".join(combos) + " |")
    w("|---|" + "---|" * len(combos))
    for tp in TP_MODES:
        cells = []
        for c in combos:
            r = base[(base.combo == c) & (base.tp_mode == tp)].iloc[0]
            cells.append(f"{pf(r.all_pf)} ({pf(r.a_pf)} / {pf(r.b_pf)})")
        w(f"| {TP_LABEL[tp]} | " + " | ".join(cells) + " |")
    w("| 取引回数 | " + " | ".join(str(int(base[(base.combo == c)].all_trades.iloc[0])) for c in combos) + " |\n")

    w("## 前半・後半ともに黒字だった設定の割合\n")
    w("フィルタ・利確方法の全組み合わせ (時間足ごとに 2,880 通り) のうち、前半・後半それぞれ20回以上取引して両方 PF>1 だった数。\n")
    w("| 銘柄・時間足 | 合格 | 割合 | PF 中央値 |")
    w("|---|---|---|---|")
    for c, r in surv_tab.iterrows():
        w(f"| {c} | {int(r.ok)} / {int(r.n)} | {r.ok / r.n * 100:.1f}% | {r.pf:.2f} |")
    w("")

    w("## フィルタの効果 (PF 中央値)\n")
    w("| 条件 | 値 | 金 | EURUSD | GBPUSD |")
    w("|---|---|---|---|---|")
    for k in FILTER_KEYS:
        t = df.pivot_table(index=k, columns="symbol", values="all_pf", aggfunc="median")
        for v, r in t.iterrows():
            w(f"| {label[k]} | {vlabel(k, v)} | {r.get('XAUUSD', np.nan):.2f} | {r.get('EURUSD', np.nan):.2f} | "
              f"{r.get('GBPUSD', np.nan):.2f} |")
    w("")
    w("* 画像で「精度が極めて高い」とされる陽線包み足・20EMA・フィボの条件は、どれも成績をはっきり改善しなかった。取引回数が減るだけのことが多い。")
    w("* 緊急損切 (画像にはない) は、ない時より成績が良かった。CCI の損切ルールだけだと急落時に損失が膨らむ。\n")

    w("## 注意\n")
    w("* 金のデータは約6か月分 (取引約50回) しかなく、推奨設定は複数の候補から選んでいる。偶然の可能性は残るので、"
      "**MT5 ストラテジーテスターで数年分の金データを使った検証と、デモ口座でのフォワードテストを必ず行うこと。**")
    w("* MQL5 の EA はこの環境でコンパイル・実行できないため未検証。ロジックは Python 版と同じになるように書いた。")
    w("* 金の後半 (7〜9月) は上昇相場で、買いのみのこの手法には追い風だった。前半 (下落局面) の PF は 1.2 程度。")
    w("* 1分足から作った足で検証しているため、ブローカーの足 (サーバー時間) とは区切りが少しずれる。")
    w("* スワップは日付をまたいだ日数 × 1日分で計算 (水曜3倍は無視)。")
    (OUT / "results.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # ------------------------------------------------ equity.png
    fig, axes = plt.subplots(2, 1, figsize=(11, 8.5))
    ax = axes[0]
    for tp, t in trades_by_tp.items():
        if len(t) == 0:
            continue
        lw, a = (2.4, 1.0) if tp == p.tp_mode else (1.0, 0.55)
        ax.step(t.exit_time, t.pnl_jpy.cumsum(), where="post", lw=lw, alpha=a, label=tp)
    ax.axvline(split, color="gray", ls=":", lw=1)
    ax.axhline(0, color="black", lw=0.6)
    ax.set_title(f"XAUUSD {p.ltf}/{p.htf} 0.01 lot: cumulative P/L by take-profit rule (JPY)")
    ax.legend(ncol=5, fontsize=8)
    ax = axes[1]
    for c in [c for c in combos if not c.startswith("XAUUSD")]:
        s, ltf, htf = c.replace("/", " ").split()
        q = Params(ltf=ltf, htf=htf, tp_mode=p.tp_mode, **BASE)
        t = run(frame(s, ltf, htf), q, SYMBOLS[s])
        ax.plot(t.exit_time, t.pnl_jpy.cumsum(), lw=1.2, label=c)
    ax.axhline(0, color="black", lw=0.6)
    ax.set_title(f"EURUSD / GBPUSD 0.01 lot, same rules (TP = {p.tp_mode}): cumulative P/L (JPY)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "equity.png", dpi=110)
    print((OUT / "results.md").read_text()[:3000])


if __name__ == "__main__":
    main()
