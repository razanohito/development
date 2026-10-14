"""金(XAUUSD)ナンピンEAのバックテストエンジン。

mql5/GoldNanpinEA.mq5 と同じロジックを 1 分足 OHLC で再現する。

ロジック概要
------------
* 売買方向: 買いのみ / 売りのみ / 両方 (買いバスケットと売りバスケットを独立管理)
* バスケットが空なら初期ロットでエントリー (トレンドフィルタが許可した場合のみ)
* 直近エントリー価格から「ナンピン幅」だけ逆行したら、
  ロット = 初期ロット x 倍率^(本数) で追加 (最大本数まで)
* バスケットの合計損益 (スワップ込み) が利確目標に達したら一括決済
* 口座全体の損切り: 含み損が残高の X% に達したら全決済し、一定時間新規停止
* 証拠金維持率がストップアウト水準を割ったら全決済 (ロスカット)

価格の扱い
----------
* データの価格は Bid。Ask = Bid + spread。買いは Ask で建て Bid で決済、売りは逆。
* 1 分足内の経路は 始値→安値→高値→終値 (陽線) / 始値→高値→安値→終値 (陰線)
  と仮定し、経路上で到達した水準で約定させる。
* 前の足の終値→次の足の始値はギャップとして扱い、到達した注文は始値で約定 (不利側)。
* 損益は USD で計算し、レポート時に固定 USDJPY で円換算する。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd
from numba import njit, prange

CONTRACT = 100.0  # 1 lot = 100 oz

DIR_BUY, DIR_SELL, DIR_BOTH = 0, 1, 2
DIR_NAMES = {DIR_BUY: "買いのみ", DIR_SELL: "売りのみ", DIR_BOTH: "両方"}


@dataclass
class Market:
    spread: float = 0.30          # USD (30 points)
    commission_per_lot: float = 0.0  # USD per lot round turn
    swap_long: float = -50.0      # USD per lot per day (買い)
    swap_short: float = -10.0     # USD per lot per day (売り)
    leverage: float = 100.0
    stopout_level: float = 0.5    # 証拠金維持率 50% でロスカット
    usdjpy: float = 150.0
    balance_jpy: float = 5_000_000.0


@dataclass
class Params:
    direction: int = DIR_BUY
    init_lot: float = 0.01
    multiplier: float = 1.2
    grid_step: float = 5.0        # USD
    max_pos: int = 20
    tp_move: float = 10.0         # 利確目標 = 初期ロットが tp_move ドル動いた分の利益
    stop_pct: float = 0.0         # 口座全体の損切り (残高比, 0 = 無効)
    cooldown_min: int = 1440      # 損切り後の新規停止時間 (分)
    filter_id: int = 0            # 0 = なし, 1 以降 = load_filters の順


# ---------------------------------------------------------------------------
# データ
# ---------------------------------------------------------------------------

def load_m1(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    return df


FILTERS = [
    ("なし", None, None),
    ("H1 SMA200", "1h", 200),
    ("H4 SMA100", "4h", 100),
]


def trend_state(df: pd.DataFrame, rule: str, period: int) -> np.ndarray:
    """各 1 分足の始値時点で確定済みの上位足終値と SMA の位置関係。
    +1: 終値 > SMA (買い許可), -1: 終値 < SMA (売り許可), 0: 未確定。"""
    s = df.set_index("datetime")["close"]
    htf = s.resample(rule, label="left", closed="left").last().dropna()
    sma = htf.rolling(period).mean()
    state = np.sign(htf - sma).fillna(0.0)
    avail = pd.DataFrame({
        "t": htf.index + pd.Timedelta(rule),  # 足が確定した時刻
        "state": state.values,
    })
    m = pd.merge_asof(df[["datetime"]], avail, left_on="datetime", right_on="t",
                      direction="backward")
    return m["state"].fillna(0.0).to_numpy(np.int8)


def build_arrays(df: pd.DataFrame):
    t = df["datetime"]
    o = df["open"].to_numpy(np.float64)
    h = df["high"].to_numpy(np.float64)
    l = df["low"].to_numpy(np.float64)
    c = df["close"].to_numpy(np.float64)
    # スワップ付与回数: 21:00 UTC を跨いだら 1 日 (水曜は 3 日分)
    shifted = t - pd.Timedelta(hours=21)
    day = shifted.dt.floor("D")
    newday = np.zeros(len(df), np.int8)
    d = day.to_numpy()
    chg = np.r_[False, d[1:] != d[:-1]]
    wd = day.dt.weekday.to_numpy()
    # 新しい取引日に入った最初の足で、前の取引日の分を付与 (水曜分は 3 倍)
    prev_wd = np.r_[0, wd[:-1]]
    newday[chg] = np.where(prev_wd[chg] == 2, 3, 1)
    epoch = pd.Timestamp("1970-01-01", tz=t.dt.tz)
    minutes = ((t - epoch).dt.total_seconds() // 60).to_numpy(np.int64)
    filt = np.zeros((len(FILTERS), len(df)), np.int8)
    for k, (_, rule, period) in enumerate(FILTERS):
        if rule is None:
            filt[k, :] = 2  # 2 = 常に許可
        else:
            filt[k, :] = trend_state(df, rule, period)
    return o, h, l, c, newday, minutes, filt


# ---------------------------------------------------------------------------
# エンジン (numba)
# ---------------------------------------------------------------------------

@njit(cache=True)
def _lot(init_lot, mult, n):
    x = init_lot * mult ** n
    v = math.floor(x / 0.01 + 0.5 + 1e-9) * 0.01
    if v < 0.01:
        v = 0.01
    return v


@njit(cache=True)
def simulate(o, h, l, c, newday, minutes, filt_row, i0, i1,
             direction, init_lot, mult, step, max_pos, tp_move, stop_pct,
             cooldown_min, spread, comm, swap_long, swap_short, leverage,
             stopout, balance0, record_curve):
    """[i0, i1) の区間をシミュレーションする。

    戻り値: stats 配列と (record_curve 時) 足ごとの終値時点エクイティ。
    stats = [最終残高, 最大DD率, 最低エクイティ, TP回数, 損切り回数, ロスカット回数,
             最大同時本数, 総取引数, 総利益, 総損失, 最大合計ロット, スワップ合計]
    """
    INF = 1e18
    tp_usd = tp_move * init_lot * CONTRACT

    bal = balance0
    # 買いバスケット
    b_n = 0; b_lots = 0.0; b_cost = 0.0; b_last = 0.0; b_swap = 0.0
    # 売りバスケット
    s_n = 0; s_lots = 0.0; s_cost = 0.0; s_last = 0.0; s_swap = 0.0

    peak = balance0
    maxdd = 0.0
    min_eq = balance0
    n_tp = 0; n_stop = 0; n_so = 0; max_n = 0; n_trades = 0
    gross_p = 0.0; gross_l = 0.0; max_lots = 0.0; swap_total = 0.0
    cool_until = -1
    use_buy = direction == DIR_BUY or direction == DIR_BOTH
    use_sell = direction == DIR_SELL or direction == DIR_BOTH

    curve = np.empty(i1 - i0 if record_curve else 1)

    path = np.empty(5)
    prev_c = o[i0]

    for i in range(i0, i1):
        # --- 日替わりスワップ ---
        if newday[i] > 0:
            if b_n > 0:
                sw = swap_long * b_lots * newday[i]
                b_swap += sw; swap_total += sw
            if s_n > 0:
                sw = swap_short * s_lots * newday[i]
                s_swap += sw; swap_total += sw

        fs = filt_row[i]
        allow_buy = use_buy and (fs == 2 or fs == 1)
        allow_sell = use_sell and (fs == 2 or fs == -1)
        now = minutes[i]

        # 経路: 前終値 -> 始値 (ギャップ) -> 高安 -> 終値
        path[0] = prev_c
        path[1] = o[i]
        if c[i] >= o[i]:
            path[2] = l[i]; path[3] = h[i]
        else:
            path[2] = h[i]; path[3] = l[i]
        path[4] = c[i]

        for sgi in range(4):
            a = path[sgi]
            bnd = path[sgi + 1]
            gap = sgi == 0
            p = a
            guard = 0
            while True:
                guard += 1
                if guard > 10000:
                    break
                # --- 新規エントリー (空のバスケット) ---
                if now >= cool_until:
                    if allow_buy and b_n == 0:
                        lot = _lot(init_lot, mult, 0)
                        b_n = 1; b_lots = lot; b_cost = lot * (p + spread); b_last = p; b_swap = 0.0
                        bal -= comm * lot; n_trades += 1
                    if allow_sell and s_n == 0:
                        lot = _lot(init_lot, mult, 0)
                        s_n = 1; s_lots = lot; s_cost = lot * p; s_last = p; s_swap = 0.0
                        bal -= comm * lot; n_trades += 1
                if b_n > max_n: max_n = b_n
                if s_n > max_n: max_n = s_n
                if b_lots + s_lots > max_lots: max_lots = b_lots + s_lots

                # エクイティ(p) = k0 + k1 * p
                k1 = CONTRACT * (b_lots - s_lots)
                k0 = bal + b_swap + s_swap + CONTRACT * (-b_cost + s_cost - s_lots * spread)
                eq = k0 + k1 * p
                if eq > peak: peak = eq
                if eq < min_eq: min_eq = eq
                dd = (peak - eq) / peak
                if dd > maxdd: maxdd = dd

                if p == bnd and not gap:
                    break
                down = bnd < a
                # --- 次のイベント水準を探す ---
                ev = -1
                best = -INF if down else INF
                lv = 0.0
                # 買いTP (上昇時)
                if b_n > 0 and not down:
                    # 決済は Bid: 利益 = (lots*Bid - cost)*100 + swap
                    lv = (b_cost * CONTRACT + tp_usd - b_swap) / (CONTRACT * b_lots)
                    if lv < best and lv <= bnd:
                        best = lv; ev = 0
                # 売りTP (下落時): 利益 = (cost - lots*(Bid+spread))*100 + swap
                if s_n > 0 and down:
                    lv = (s_cost * CONTRACT - tp_usd + s_swap) / (CONTRACT * s_lots) - spread
                    if lv > best and lv >= bnd:
                        best = lv; ev = 1
                # 買いナンピン (下落時)
                if b_n > 0 and b_n < max_pos and down:
                    lv = b_last - step
                    if lv > best and lv >= bnd:
                        best = lv; ev = 2
                # 売りナンピン (上昇時)
                if s_n > 0 and s_n < max_pos and not down:
                    lv = s_last + step
                    if lv < best and lv <= bnd:
                        best = lv; ev = 3
                # 口座損切り / ロスカット: エクイティが下がる方向のみ
                if (k1 > 0 and down) or (k1 < 0 and not down):
                    if stop_pct > 0.0:
                        lv = (bal * (1.0 - stop_pct) - k0) / k1
                        if (down and lv > best and lv >= bnd) or ((not down) and lv < best and lv <= bnd):
                            best = lv; ev = 4
                    # ロスカット: eq <= stopout * 必要証拠金(p)
                    m1 = stopout * CONTRACT * (b_lots + s_lots) / leverage
                    den = k1 - m1
                    if den != 0.0:
                        lv = -k0 / den
                        if (down and lv > best and lv >= bnd) or ((not down) and lv < best and lv <= bnd):
                            best = lv; ev = 5
                if ev < 0:
                    # イベントなし: 区間終点まで進む
                    if p == bnd:
                        break
                    p = bnd
                    gap = False
                    continue
                # ギャップ中は始値 (区間終点) で約定
                fill = bnd if gap else best
                # 現在価格より手前の水準 (既に越えている) は現在価格で約定
                if down and best > p:
                    fill = p
                if (not down) and best < p:
                    fill = p
                p = fill
                if ev == 0 or ev == 4 or ev == 5:
                    # 買いバスケット決済
                    if ev == 0 or b_n > 0:
                        if b_n > 0:
                            pnl = CONTRACT * (b_lots * p - b_cost) + b_swap - comm * b_lots
                            bal += pnl
                            if pnl >= 0: gross_p += pnl
                            else: gross_l -= pnl
                            b_n = 0; b_lots = 0.0; b_cost = 0.0; b_swap = 0.0
                    if ev == 0:
                        n_tp += 1
                if ev == 1 or ev == 4 or ev == 5:
                    if s_n > 0:
                        pnl = CONTRACT * (s_cost - s_lots * (p + spread)) + s_swap - comm * s_lots
                        bal += pnl
                        if pnl >= 0: gross_p += pnl
                        else: gross_l -= pnl
                        s_n = 0; s_lots = 0.0; s_cost = 0.0; s_swap = 0.0
                    if ev == 1:
                        n_tp += 1
                if ev == 4:
                    n_stop += 1
                    cool_until = now + cooldown_min
                if ev == 5:
                    n_so += 1
                    cool_until = now + cooldown_min
                if ev == 2:
                    lot = _lot(init_lot, mult, b_n)
                    b_n += 1; b_lots += lot; b_cost += lot * (p + spread); b_last = p
                    bal -= comm * lot; n_trades += 1
                if ev == 3:
                    lot = _lot(init_lot, mult, s_n)
                    s_n += 1; s_lots += lot; s_cost += lot * p; s_last = p
                    bal -= comm * lot; n_trades += 1
                if bal <= 0:
                    break
            if bal <= 0:
                break
        prev_c = c[i]
        if record_curve:
            k1 = CONTRACT * (b_lots - s_lots)
            k0 = bal + b_swap + s_swap + CONTRACT * (-b_cost + s_cost - s_lots * spread)
            curve[i - i0] = k0 + k1 * c[i]
        if bal <= 0:
            if record_curve:
                for j in range(i + 1, i1):
                    curve[j - i0] = 0.0
            break

    # 期間終了時点の含み損益も込みで最終エクイティとする
    k1 = CONTRACT * (b_lots - s_lots)
    k0 = bal + b_swap + s_swap + CONTRACT * (-b_cost + s_cost - s_lots * spread)
    final_eq = k0 + k1 * c[i1 - 1] if bal > 0 else 0.0
    stats = np.array([final_eq, maxdd, min_eq, n_tp, n_stop, n_so, max_n, n_trades,
                      gross_p, gross_l, max_lots, swap_total, bal])
    return stats, curve


STAT_NAMES = ["final_eq", "maxdd", "min_eq", "n_tp", "n_stop", "n_stopout", "max_pos_used",
              "n_trades", "gross_profit", "gross_loss", "max_lots", "swap", "final_balance"]


@njit(parallel=True, cache=True)
def simulate_many(o, h, l, c, newday, minutes, filt, i0, i1, grid,
                  spread, comm, swap_long, swap_short, leverage, stopout, balance0):
    """grid: (N, 9) = direction, init_lot, mult, step, max_pos, tp_move, stop_pct, cooldown, filter_id"""
    n = grid.shape[0]
    out = np.empty((n, 13))
    for k in prange(n):
        g = grid[k]
        st, _ = simulate(o, h, l, c, newday, minutes, filt[int(g[8])], i0, i1,
                         int(g[0]), g[1], g[2], g[3], int(g[4]), g[5], g[6], int(g[7]),
                         spread, comm, swap_long, swap_short, leverage, stopout, balance0, False)
        out[k] = st
    return out


class Backtester:
    def __init__(self, df: pd.DataFrame, market: Market | None = None):
        self.df = df
        self.m = market or Market()
        self.o, self.h, self.l, self.c, self.newday, self.minutes, self.filt = build_arrays(df)
        self.balance_usd = self.m.balance_jpy / self.m.usdjpy

    def index_range(self, start=None, end=None):
        t = self.df["datetime"]
        i0 = 0 if start is None else int(np.searchsorted(t.values, np.datetime64(pd.Timestamp(start).tz_convert(None) if pd.Timestamp(start).tzinfo else pd.Timestamp(start))))
        i1 = len(t) if end is None else int(np.searchsorted(t.values, np.datetime64(pd.Timestamp(end).tz_convert(None) if pd.Timestamp(end).tzinfo else pd.Timestamp(end))))
        return i0, i1

    def run(self, p: Params, start=None, end=None, curve=False):
        i0, i1 = self.index_range(start, end)
        m = self.m
        st, cv = simulate(self.o, self.h, self.l, self.c, self.newday, self.minutes,
                          self.filt[p.filter_id], i0, i1, p.direction, p.init_lot, p.multiplier,
                          p.grid_step, p.max_pos, p.tp_move, p.stop_pct, p.cooldown_min,
                          m.spread, m.commission_per_lot, m.swap_long, m.swap_short,
                          m.leverage, m.stopout_level, self.balance_usd, curve)
        res = self._to_dict(st)
        if curve:
            res["curve"] = pd.Series(cv * m.usdjpy, index=self.df["datetime"].iloc[i0:i1].values)
        return res

    def run_grid(self, grid: np.ndarray, start=None, end=None) -> pd.DataFrame:
        i0, i1 = self.index_range(start, end)
        m = self.m
        out = simulate_many(self.o, self.h, self.l, self.c, self.newday, self.minutes, self.filt,
                            i0, i1, grid.astype(np.float64), m.spread, m.commission_per_lot,
                            m.swap_long, m.swap_short, m.leverage, m.stopout_level, self.balance_usd)
        return pd.DataFrame([self._to_dict(r) for r in out])

    def _to_dict(self, st):
        fx = self.m.usdjpy
        d = dict(zip(STAT_NAMES, st))
        b0 = self.balance_usd
        return {
            "profit_jpy": (d["final_eq"] - b0) * fx,
            "return_pct": (d["final_eq"] / b0 - 1) * 100,
            "maxdd_pct": d["maxdd"] * 100,
            "n_tp": int(d["n_tp"]),
            "n_stop": int(d["n_stop"]),
            "n_stopout": int(d["n_stopout"]),
            "max_pos_used": int(d["max_pos_used"]),
            "max_lots": round(d["max_lots"], 2),
            "pf": d["gross_profit"] / d["gross_loss"] if d["gross_loss"] > 0 else float("inf"),
            "swap_jpy": d["swap"] * fx,
            "open_pnl_jpy": (d["final_eq"] - d["final_balance"]) * fx,
        }
