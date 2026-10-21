"""推奨設定 (EA の初期値) の長期テスト。

1) 最新 8 か月 (1 分足 2026-02-01〜09-25)。2026-04-20 より前は最適化に使っていない期間 (検証用)。
2) 1 年通し (15 分足 2021-03-05〜2022-03-04)。金価格が今の約 4 割だったので、
   ドル幅の設定 (押し目・幅・利確・損切り) を価格の比で縮めた値と、そのままの値の両方で走らせる。
   15 分足の中の動きは 始値→安値→高値→終値 (陽線) とみなすので、1 分足より粗い近似。
   近似の偏りは、2026 年の 1 分足を 15 分足にまとめて走らせた結果と比べて確かめる。

    python3 fetch_year_data.py
    python3 yeartest.py      # results/yeartest.md, equity_year.png
"""
from __future__ import annotations

import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["font.family"] = ["IPAGothic", "DejaVu Sans"]
import numpy as np
import pandas as pd

import dipstack_bt as bt

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "results"
USDJPY = 158.0
SPREAD, SWAP = 0.30, 30.0
LOTS, PER_LEVEL = 0.01, 3
OPT_START = pd.Timestamp("2026-04-20")   # 最適化に使った取引期間の始まり
YEAR = (pd.Timestamp("2021-03-05"), pd.Timestamp("2022-03-05"))

PICK = dict(lookback=480, dip=60.0, step=15.0, max_levels=2, tp=3.0, sl=40.0, trend=2, hold_h=24, cool_h=24)
OLD = dict(lookback=60, dip=20.0, step=15.0, max_levels=3, tp=5.0, sl=30.0, trend=0, hold_h=0, cool_h=0)
PRICE_KEYS = ("dip", "step", "tp", "sl")


def load_m1_long() -> pd.DataFrame:
    df = pd.read_csv(DATA / "XAUUSD_1m_long.csv.gz")
    df["time"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_localize(None)
    return df[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)


def load_m15_ej() -> pd.DataFrame:
    df = pd.read_csv(DATA / "XAUUSD_m15_ej.csv.gz")
    df["time"] = pd.to_datetime(df["Date"])
    for k in ("open", "high", "low", "close"):
        df[k] = df[k] / 100.0
    return df[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)


def to_m15(m1: pd.DataFrame) -> pd.DataFrame:
    g = m1.set_index("time").resample("15min")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last()}).dropna()
    return out.reset_index()


def run(df: pd.DataFrame, p: dict, start: pd.Timestamp, end: pd.Timestamp, bar_min: int = 1,
        spread: float = SPREAD):
    arrs = bt.prepare(df)
    roll_high = df["high"].rolling(max(1, p["lookback"] // bar_min), min_periods=1).max().shift(1).to_numpy()
    trend = (bt.trend_series(df, "1h", 50, "ema") if p["trend"] == 1 else
             bt.trend_series(df, "4h", 100, "sma"))
    t = df["time"].to_numpy()
    s, e = int(np.searchsorted(t, np.datetime64(start))), int(np.searchsorted(t, np.datetime64(end)))
    pnl, dd, maxlots, eq, span = bt.run(*arrs, roll_high, trend, LOTS, PER_LEVEL, p["dip"], p["step"],
                                        int(p["max_levels"]), p["tp"], p["sl"], 1 if p["trend"] else 0,
                                        spread, SWAP, s, e, bt.minutes(df), int(p["hold_h"] * 60),
                                        int(p["cool_h"] * 60))
    close_t = df["time"].iloc[span[:, 1]].to_numpy() if len(pnl) else np.array([], dtype="datetime64[ns]")
    return dict(pnl=pnl * USDJPY, dd=dd * USDJPY, eq=pd.Series(eq[s:e] * USDJPY, index=df["time"].iloc[s:e]),
                close_t=close_t)


def stats(r) -> dict:
    p = r["pnl"]
    gp, gl = p[p > 0].sum(), -p[p < 0].sum()
    return dict(n=len(p), win=(p > 0).mean() if len(p) else np.nan,
                pf=gp / gl if gl > 0 else np.inf, net=p.sum(), dd=r["dd"], worst=p.min() if len(p) else 0.0)


def scaled(p: dict, f: float) -> dict:
    q = dict(p)
    for k in PRICE_KEYS:
        q[k] = round(p[k] * f, 2)
    return q


def monthly(r) -> pd.Series:
    s = pd.Series(r["pnl"], index=pd.to_datetime(r["close_t"]))
    return s.groupby(s.index.to_period("M")).sum()


def man(x: float) -> str:
    return f"{x / 1e4:+,.1f}万"


def row(name: str, st: dict) -> str:
    pf = "負けなし" if np.isinf(st["pf"]) else f"{st['pf']:.2f}"
    win = "-" if np.isnan(st["win"]) else f"{st['win']:.0%}"
    return (f"| {name} | {st['n']} | {win} | {pf} | {man(st['net'])} | {st['dd'] / 1e4:,.1f}万 "
            f"| {man(st['worst'])} |")


HEAD = "| 設定・期間 | 回数 | 勝率 | PF | 純損益 | 最大DD | 最悪1回 |\n|---|---|---|---|---|---|---|"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    m1 = load_m1_long()
    ej = load_m15_ej()
    end26 = m1["time"].iloc[-1] + pd.Timedelta(minutes=1)
    # H4 SMA100 は 100 本 (400 時間) 揃うまで使えないので、データ先頭から約 4 週間はウォームアップ
    warm = m1["time"].iloc[0] + pd.Timedelta(weeks=4)

    lines = []
    # --- 1) 最新 8 か月 (1 分足)
    res26 = {}
    for name, p in [("推奨", PICK), ("旧初期値", OLD)]:
        for seg, (a, b) in [("全期間", (warm, end26)), ("検証 (最適化に未使用)", (warm, OPT_START)),
                            ("最適化に使った期間", (OPT_START, end26))]:
            r = run(m1, p, a, b)
            res26[(name, seg)] = r
            lines.append(row(f"{name} / {seg}", stats(r)))
    sec1 = "\n".join(lines)

    # 最適化で「頑健」とした候補 (optimize.py の最後の絞り込みまで残った設定) は検証期間でどうだったか
    cand = pd.read_csv(OUT / "opt_candidates.csv")
    rob = cand[(cand.win_pos >= 12) & (cand.bad_net > 0) & (cand.nb_rate >= 0.6)].copy()
    rob = (rob.assign(ratio=rob.full_net / rob.full_dd).sort_values(["ratio", "nb_rate"], ascending=False)
           .drop_duplicates(["full_net", "full_dd", "full_n", "h1_net"]))
    keys = list(PICK)
    rob_oos = np.array([stats(run(m1, {k: (int(v) if k in ("lookback", "trend", "max_levels") else v)
                                       for k, v in q.items()}, warm, OPT_START))["net"]
                        for q in rob[keys].to_dict("records")])

    # --- 2) 1 年通し (15 分足, 2021-03〜2022-03)
    px_now = m1.loc[m1.time >= OPT_START, "close"].mean()
    ej_year = ej[(ej.time >= YEAR[0]) & (ej.time < YEAR[1])]
    px_then = ej_year["close"].mean()
    f = px_then / px_now
    pick_s = scaled(PICK, f)
    old_s = scaled(OLD, f)
    # 近似の偏り: 同じ 2026 年の期間を 1 分足と 15 分足で比べる
    m15_26 = to_m15(m1)
    cal_m1 = stats(run(m1, PICK, warm, end26))
    cal_m15 = stats(run(m15_26, PICK, warm, end26, bar_min=15))
    yr = {}
    lines = []
    for name, p, sp in [("推奨 (価格比で縮小)", pick_s, SPREAD), ("推奨 (価格比で縮小, スプレッド $0.15)", pick_s, 0.15),
                        ("推奨 (ドル幅そのまま)", PICK, SPREAD), ("旧初期値 (価格比で縮小)", old_s, SPREAD)]:
        r = run(ej, p, YEAR[0], YEAR[1], bar_min=15, spread=sp)
        yr[name] = r
        lines.append(row(name, stats(r)))
    sec2 = "\n".join(lines)
    n_bars = len(ej_year)
    full_bars = (YEAR[1] - YEAR[0]).days * 5 / 7 * 23 * 4

    # 月別
    mon26 = monthly(res26[("推奨", "全期間")])
    mon_old26 = monthly(res26[("旧初期値", "全期間")])
    mon_yr = monthly(yr["推奨 (価格比で縮小)"])
    mon_yr_old = monthly(yr["旧初期値 (価格比で縮小)"])

    def mon_table(a: pd.Series, b: pd.Series) -> str:
        idx = sorted(set(a.index) | set(b.index))
        out = ["| 月 | 推奨 | 旧初期値 |", "|---|---|---|"]
        for m in idx:
            out.append(f"| {m} | {man(a.get(m, 0.0))} | {man(b.get(m, 0.0))} |")
        out.append(f"| 黒字の月 | {(a > 0).sum()}/{len(idx)} | {(b > 0).sum()}/{len(idx)} |")
        return "\n".join(out)

    fig, axes = plt.subplots(2, 1, figsize=(10, 8))
    for ax, data, rs, title in [
        (axes[0], m1, [(res26[("推奨", "全期間")], "推奨"), (res26[("旧初期値", "全期間")], "旧初期値")],
         "最新 8 か月 (1 分足 2026-03〜09)。灰色の縦線より左は最適化に使っていない期間"),
        (axes[1], ej, [(yr["推奨 (価格比で縮小)"], "推奨 (価格比で縮小)"), (yr["旧初期値 (価格比で縮小)"], "旧初期値 (価格比で縮小)")],
         "1 年通し (15 分足 2021-03〜2022-03, 近似)")]:
        for r, lab in rs:
            ax.plot(r["eq"].index, r["eq"].values / 1e4, lw=1, label=lab)
        ax.axhline(0, color="0.4", lw=0.5)
        ax.set_ylabel("含み損込み損益 (万円)")
        ax.set_title(title, fontsize=10)
        ax.legend(loc="lower left")
        a, b = rs[0][0]["eq"].index[0], rs[0][0]["eq"].index[-1]
        seg = data[(data.time >= a) & (data.time <= b)]
        ax2 = ax.twinx()
        ax2.plot(seg["time"], seg["close"], color="0.75", lw=0.6)
        ax2.set_ylabel("金価格 (USD)")
    axes[0].axvline(OPT_START, color="0.5", lw=1, ls="--")
    fig.tight_layout()
    fig.savefig(OUT / "equity_year.png", dpi=110)

    ps = " / ".join(f"{k} ${pick_s[k]:g}" for k in PRICE_KEYS)
    md = f"""# 推奨設定の長期テスト

推奨設定 = EA の初期値 (高値 480 分 / 押し目 $60 / 幅 $15 / 最大 2 段 / 利確 $3 / 損切り $40 / H4 SMA100 / 時間切れ 24 時間 / 休み 24 時間)。
{LOTS} ロット x {PER_LEVEL} 本/段、スプレッド ${SPREAD:.2f}、買いスワップ ${SWAP:.0f}/ロット/日、USDJPY {USDJPY:.0f} 固定。

## 1. 最新 8 か月 (1 分足 {m1.time.iloc[0]:%Y-%m-%d}〜{m1.time.iloc[-1]:%Y-%m-%d})

最新の 1 分足は getdata-finance の無料サンプルの履歴をつないだ約 8 か月が上限 (1 年分は取得できなかった)。
H4 SMA100 のウォームアップ後の {warm:%Y-%m-%d} から取引。{OPT_START:%Y-%m-%d} より前は最適化に使っていない検証期間。

{HEAD}
{sec1}

最適化で頑健とした {len(rob)} 通りのうち、検証期間でも黒字だったのは {(rob_oos > 0).sum()} 通り。

**月別 (全期間)**

{mon_table(mon26, mon_old26)}

## 2. 1 年通し (15 分足 {YEAR[0]:%Y-%m-%d}〜{(YEAR[1] - pd.Timedelta(days=1)):%Y-%m-%d}, 近似)

ejtraderLabs/historical-data の 15 分足 ({n_bars:,} 本、取引時間の約 {n_bars / full_bars:.0%})。この期間の金の平均は ${px_then:,.0f}、最適化期間は ${px_now:,.0f}。
価格比 {f:.3f} でドル幅を縮めた推奨設定: {ps}。

{HEAD}
{sec2}

**近似の偏り**: 2026 年の同じ期間を 15 分足にまとめて走らせると 純損益 {man(cal_m15['net'])} / 最大DD {cal_m15['dd'] / 1e4:,.1f}万 ({cal_m15['n']} 回)、
1 分足では {man(cal_m1['net'])} / {cal_m1['dd'] / 1e4:,.1f}万 ({cal_m1['n']} 回)。

**月別 (価格比で縮小)**

{mon_table(mon_yr, mon_yr_old)}

![equity](equity_year.png)
"""
    (OUT / "yeartest.md").write_text(md)
    print(md)


if __name__ == "__main__":
    main()
