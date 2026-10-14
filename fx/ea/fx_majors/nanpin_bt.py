"""ナンピン(グリッド・マーチン)EA の Python バックテスター (EURUSD / GBPUSD などドルストレート用)。

EA のロジック (fx/ea/mql5/GoldNanpinEA.mq5 の仕様に合わせたもの。括弧内は EA の入力名):
  * バスケットにポジションが無ければ、バー始値で初期ロット (InpInitLot) を建てる。
    トレンドフィルタ有効時は、確定した 4時間足の終値が 100SMA より上なら買い、下なら売りのみ新規
    (InpFilterTF=H4 / InpFilterMA=100)。
  * 最後の建値から「ナンピン幅」(InpGridStep) だけ逆行するたびに、ロット = 初期ロット × 倍率^n
    (InpMultiplier) で追加。最大本数 (InpMaxPositions) に達したらそれ以上は建てない。
  * バスケットの合計損益 (スワップ込み) が「初期ロットが利確幅 (InpTPMove) 動いた分の利益」に
    達したら全ポジションを一括決済。
  * 口座損切り (InpAccountStopPct): 含み損が残高の % に達したら全決済し、
    クールダウン (InpCooldownMin=1440分) の間は新規を止める。
  * 証拠金維持率がロスカット水準を割ったら全決済し、その時点で「破綻」として停止。

約定モデル:
  * データは Bid の OHLC。Ask = Bid + スプレッド。
  * バー内の値動きは 陽線なら 始→安→高→終、陰線なら 始→高→安→終 と仮定し、
    各区間内で指値水準 (ナンピン/TP/損切り/ロスカット) を跨いだらその水準で約定。
    窓開け (前バー終値→始値) で跨いだ場合は始値で約定。
  * 毎日のロールオーバーで スワップ (USD/ロット/日) を加算。水曜は 3 日分。
  * 証拠金 = ロット × 10万通貨 × 建値 × USDJPY / レバレッジ。余剰証拠金が足りない追加注文は見送り。
  * 損益は USD で計算し、固定の USDJPY で円換算 (為替差は無視)。

numba で JIT コンパイルし、パラメータ総当たりを並列で回す。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from numba import njit, prange

CONTRACT = 100_000.0
EPS = 1e-9  # 価格比較の浮動小数誤差の許容幅
MODE_BUY, MODE_SELL, MODE_BOTH = 0, 1, 2
MODE_NAMES = {MODE_BUY: "買いのみ", MODE_SELL: "売りのみ", MODE_BOTH: "両建て"}

# 結果配列の列
R_FINAL, R_MAXDD, R_BUST, R_BASKETS, R_STOPS, R_MAXPOS, R_GROSSW, R_GROSSL, R_MINML, R_BUSTBAR = range(10)
N_RESULT = 10


@dataclass
class Account:
    capital_jpy: float = 5_000_000.0
    usdjpy: float = 150.0
    leverage: float = 25.0
    stopout_level: float = 1.0  # 証拠金維持率 100% でロスカット
    spread_pips: float = 1.0
    swap_usd_per_lot_day: float = -5.0  # 売り買いとも 1 ロット 1 日 -5 USD (保守的な仮定)
    pip: float = 0.0001
    cooldown_min: float = 1440.0  # 口座損切り後に新規を止める時間 (EA の InpCooldownMin)


@dataclass
class Params:
    mode: int = MODE_BUY
    init_lot: float = 0.01
    mult: float = 1.2
    grid_pips: float = 20.0
    max_pos: int = 10
    tp_pips: float = 10.0
    stop_pct: float = 0.0  # 0 = 口座損切りなし
    use_ma: int = 0
    extra: dict = field(default_factory=dict)

    def as_row(self) -> list[float]:
        return [self.mode, self.init_lot, self.mult, self.grid_pips, self.max_pos,
                self.tp_pips, self.stop_pct, self.use_ma]


PARAM_COLS = ["mode", "init_lot", "mult", "grid_pips", "max_pos", "tp_pips", "stop_pct", "use_ma"]


def load_h1(path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"])
    return df.sort_values("time").reset_index(drop=True)


def prepare(df: pd.DataFrame, filter_hours: int = 4, filter_period: int = 100):
    """numba に渡す配列を作る。

    ma / prev_close は「そのバーの時点で確定している filter_hours 時間足」の SMA と終値 (先読みなし)。
    """
    o = df["open"].to_numpy(np.float64)
    h = df["high"].to_numpy(np.float64)
    l = df["low"].to_numpy(np.float64)
    c = df["close"].to_numpy(np.float64)
    ft = df.set_index("time")["close"].resample(f"{filter_hours}h").last().dropna()
    fsma = ft.rolling(filter_period).mean()
    bin_end = (ft.index + pd.Timedelta(hours=filter_hours)).to_numpy()
    idx = np.searchsorted(bin_end, df["time"].to_numpy(), side="right") - 1
    valid = idx >= 0
    ma = np.full(len(df), np.nan)
    prev_close = np.full(len(df), np.nan)
    ma[valid] = fsma.to_numpy()[idx[valid]]
    prev_close[valid] = ft.to_numpy()[idx[valid]]
    tmin = (df["time"].astype("int64") // 60_000_000_000).to_numpy(np.float64)
    d = df["time"].dt.normalize()
    newday = (d != d.shift(1)).to_numpy().copy()
    newday[0] = False
    # 水曜のロールオーバー (= 木曜の最初のバー) は 3 日分
    swap_days = np.where(newday, np.where(df["time"].dt.dayofweek.to_numpy() == 3, 3.0, 1.0), 0.0)
    return o, h, l, c, ma, prev_close, swap_days, tmin


@njit(cache=True)
def _lot(init_lot, mult, k):
    # MQL の NormalizeDouble(x, 2) と同じく四捨五入 (浮動小数の誤差分を少し足す)
    v = np.floor(init_lot * mult ** k * 100.0 + 0.5 + 1e-9) / 100.0
    return v if v >= 0.01 else 0.01


@njit(cache=True)
def _simulate(o, h, l, c, ma, prev_close, swap_days, tmin,
              mode, init_lot, mult, grid_pips, max_pos, tp_pips, stop_pct, use_ma,
              capital_jpy, usdjpy, leverage, stopout_level, spread_pips, swap_usd, pip, cooldown,
              start, end, eq_out):
    """1 通りの設定で [start, end) を検証する。eq_out が長さ>0 ならバー終値時点の有効証拠金(円)を書く。"""
    res = np.zeros(N_RESULT)
    spr = spread_pips * pip
    grid = grid_pips * pip
    tp = tp_pips * pip  # 初期ロットがこの幅動いた分の利益で一括決済
    mp = int(max_pos)
    # バスケット [0]=買い [1]=売り。建値は Ask(買い)/Bid(売り) で保持
    lots = np.zeros((2, mp))
    ents = np.zeros((2, mp))
    n = np.zeros(2, np.int64)
    margin = np.zeros(2)  # 円
    swb = np.zeros(2)  # バスケットごとの累積スワップ (円)
    cool_until = -1e18
    tp_money = init_lot * CONTRACT * tp * usdjpy  # 一括決済の目標利益 (円)
    cu = CONTRACT * usdjpy
    bal = capital_jpy  # 円
    peak = capital_jpy
    maxdd = 0.0
    min_ml = 1e18
    baskets = 0
    stops = 0
    maxpos = 0
    gross_w = 0.0
    gross_l = 0.0
    busted = False
    bust_bar = -1.0
    use_buy = mode == MODE_BUY or mode == MODE_BOTH
    use_sell = mode == MODE_SELL or mode == MODE_BOTH
    pts = np.zeros(5)
    last = o[start]

    for i in range(start, end):
        # --- ロールオーバーのスワップ
        if swap_days[i] > 0.0:
            for s in range(2):
                tl = 0.0
                for k in range(n[s]):
                    tl += lots[s, k]
                sw = tl * swap_usd * swap_days[i] * usdjpy
                bal += sw
                swb[s] += sw

        # --- 新規バスケット (バー始値で)
        for s in range(2):
            if n[s] > 0:
                continue
            if s == 0 and not use_buy:
                continue
            if s == 1 and not use_sell:
                continue
            if tmin[i] < cool_until:
                continue
            if use_ma == 1:
                if np.isnan(ma[i]):
                    continue
                if s == 0 and not (prev_close[i] > ma[i]):
                    continue
                if s == 1 and not (prev_close[i] < ma[i]):
                    continue
            px = o[i] + spr if s == 0 else o[i]
            lt = _lot(init_lot, mult, 0)
            need = lt * CONTRACT * px * usdjpy / leverage
            # 有効証拠金 (始値時点)
            fl = 0.0
            for ss in range(2):
                for k in range(n[ss]):
                    if ss == 0:
                        fl += lots[ss, k] * (o[i] - ents[ss, k]) * CONTRACT
                    else:
                        fl += lots[ss, k] * (ents[ss, k] - (o[i] + spr)) * CONTRACT
            eq = bal + fl * usdjpy
            if eq - margin[0] - margin[1] < need:
                continue
            lots[s, 0] = lt
            ents[s, 0] = px
            n[s] = 1
            margin[s] = need

        # --- バー内の経路
        pts[0] = last
        pts[1] = o[i]
        if c[i] >= o[i]:
            pts[2] = l[i]
            pts[3] = h[i]
        else:
            pts[2] = h[i]
            pts[3] = l[i]
        pts[4] = c[i]

        for seg in range(4):
            a = pts[seg]
            b = pts[seg + 1]
            gap = seg == 0
            if a == b:
                continue
            down = b < a
            # 買いバスケット
            if n[0] > 0:
                if down:
                    while n[0] < mp:
                        lvl = ents[0, n[0] - 1] - grid - spr  # Bid 水準
                        if b > lvl + EPS:
                            break
                        fill = b if gap else lvl
                        px = fill + spr
                        lt = _lot(init_lot, mult, n[0])
                        need = lt * CONTRACT * px * usdjpy / leverage
                        fl = 0.0
                        for ss in range(2):
                            for k in range(n[ss]):
                                if ss == 0:
                                    fl += lots[ss, k] * (fill - ents[ss, k]) * CONTRACT
                                else:
                                    fl += lots[ss, k] * (ents[ss, k] - (fill + spr)) * CONTRACT
                        if bal + fl * usdjpy - margin[0] - margin[1] < need:
                            break
                        lots[0, n[0]] = lt
                        ents[0, n[0]] = px
                        n[0] += 1
                        margin[0] += need
                else:
                    sl = 0.0
                    sv = 0.0
                    for k in range(n[0]):
                        sl += lots[0, k]
                        sv += lots[0, k] * ents[0, k]
                    # 合計損益 (スワップ込み) が目標利益に届く Bid
                    target = ((tp_money - swb[0]) / cu + sv) / sl
                    if b >= target - EPS:
                        fill = b if gap else target
                        pnl = (fill * sl - sv) * cu
                        bal += pnl
                        pnl += swb[0]
                        swb[0] = 0.0
                        if pnl >= 0:
                            gross_w += pnl
                        else:
                            gross_l -= pnl
                        baskets += 1
                        n[0] = 0
                        margin[0] = 0.0
            # 売りバスケット (建値は Bid、決済は Ask)
            if n[1] > 0:
                if not down:
                    while n[1] < mp:
                        lvl = ents[1, n[1] - 1] + grid  # Bid 水準
                        if b < lvl - EPS:
                            break
                        fill = b if gap else lvl
                        lt = _lot(init_lot, mult, n[1])
                        need = lt * CONTRACT * fill * usdjpy / leverage
                        fl = 0.0
                        for ss in range(2):
                            for k in range(n[ss]):
                                if ss == 0:
                                    fl += lots[ss, k] * (fill - ents[ss, k]) * CONTRACT
                                else:
                                    fl += lots[ss, k] * (ents[ss, k] - (fill + spr)) * CONTRACT
                        if bal + fl * usdjpy - margin[0] - margin[1] < need:
                            break
                        lots[1, n[1]] = lt
                        ents[1, n[1]] = fill
                        n[1] += 1
                        margin[1] += need
                else:
                    sl = 0.0
                    sv = 0.0
                    for k in range(n[1]):
                        sl += lots[1, k]
                        sv += lots[1, k] * ents[1, k]
                    target = (sv - (tp_money - swb[1]) / cu) / sl - spr  # Bid 水準
                    if b <= target + EPS:
                        fill = b if gap else target
                        pnl = (sv - (fill + spr) * sl) * cu
                        bal += pnl
                        pnl += swb[1]
                        swb[1] = 0.0
                        if pnl >= 0:
                            gross_w += pnl
                        else:
                            gross_l -= pnl
                        baskets += 1
                        n[1] = 0
                        margin[1] = 0.0

            tot = n[0] + n[1]
            if tot > maxpos:
                maxpos = tot
            if tot == 0:
                if bal > peak:
                    peak = bal
                continue

            # --- 有効証拠金 = K + N*p (p は Bid) の一次式
            nb = 0.0
            kb = 0.0
            for k in range(n[0]):
                nb += lots[0, k]
                kb -= lots[0, k] * ents[0, k]
            ns = 0.0
            ks = 0.0
            for k in range(n[1]):
                ns += lots[1, k]
                ks += lots[1, k] * (ents[1, k] - spr)
            N = (nb - ns) * CONTRACT * usdjpy
            K = bal + (kb + ks) * CONTRACT * usdjpy
            eq_b = K + N * b
            used = margin[0] + margin[1]
            ml = eq_b / used
            if ml < min_ml:
                min_ml = ml
            thr_so = stopout_level * used
            thr_sl = bal * (1.0 - stop_pct / 100.0) if stop_pct > 0 else -1e18
            thr = thr_so if thr_so > thr_sl else thr_sl
            if eq_b <= thr:
                # 区間内で閾値に達した価格で決済 (窓開けなら b)
                if gap or N == 0.0:
                    p_hit = b
                else:
                    p_hit = (thr - K) / N
                    lo_ = a if a < b else b
                    hi_ = b if a < b else a
                    if p_hit < lo_:
                        p_hit = lo_
                    if p_hit > hi_:
                        p_hit = hi_
                eq_hit = K + N * p_hit
                pnl = eq_hit - bal
                dd = (peak - eq_hit) / peak
                if dd > maxdd:
                    maxdd = dd
                bal = eq_hit
                gross_l += -pnl if pnl < 0 else 0.0
                n[0] = 0
                n[1] = 0
                margin[0] = 0.0
                margin[1] = 0.0
                swb[0] = 0.0
                swb[1] = 0.0
                if thr_so >= thr_sl:
                    busted = True
                    bust_bar = i
                    break
                stops += 1
                cool_until = tmin[i] + cooldown
                continue
            dd = (peak - eq_b) / peak
            if dd > maxdd:
                maxdd = dd
            if eq_b > peak:
                peak = eq_b
        if busted:
            if eq_out.shape[0] > 0:
                for j in range(i, end):
                    eq_out[j - start] = bal
            break
        last = c[i]
        if eq_out.shape[0] > 0:
            fl = 0.0
            for k in range(n[0]):
                fl += lots[0, k] * (c[i] - ents[0, k]) * CONTRACT
            for k in range(n[1]):
                fl += lots[1, k] * (ents[1, k] - (c[i] + spr)) * CONTRACT
            eq_out[i - start] = bal + fl * usdjpy

    # 期間終了時は含み損益を時価で確定
    fl = 0.0
    ce = c[end - 1]
    for k in range(n[0]):
        fl += lots[0, k] * (ce - ents[0, k]) * CONTRACT
    for k in range(n[1]):
        fl += lots[1, k] * (ents[1, k] - (ce + spr)) * CONTRACT
    if not busted:
        bal += fl * usdjpy
    res[R_FINAL] = bal
    res[R_MAXDD] = maxdd
    res[R_BUST] = 1.0 if busted else 0.0
    res[R_BASKETS] = baskets
    res[R_STOPS] = stops
    res[R_MAXPOS] = maxpos
    res[R_GROSSW] = gross_w
    res[R_GROSSL] = gross_l
    res[R_MINML] = min_ml
    res[R_BUSTBAR] = bust_bar
    return res


@njit(parallel=True, cache=True)
def _run_grid(o, h, l, c, ma, prev_close, swap_days, tmin, P,
              capital_jpy, usdjpy, leverage, stopout_level, spread_pips, swap_usd, pip, cooldown, start, end):
    m = P.shape[0]
    out = np.zeros((m, N_RESULT))
    empty = np.zeros(0)
    for j in prange(m):
        out[j] = _simulate(o, h, l, c, ma, prev_close, swap_days, tmin,
                           int(P[j, 0]), P[j, 1], P[j, 2], P[j, 3], P[j, 4], P[j, 5], P[j, 6], int(P[j, 7]),
                           capital_jpy, usdjpy, leverage, stopout_level, spread_pips, swap_usd, pip, cooldown,
                           start, end, empty)
    return out


def _to_frame(P: np.ndarray, R: np.ndarray, acct: Account) -> pd.DataFrame:
    df = pd.DataFrame(P, columns=PARAM_COLS)
    for col in ("mode", "max_pos", "use_ma"):
        df[col] = df[col].astype(int)
    df["final_jpy"] = R[:, R_FINAL].round(0)
    df["profit_jpy"] = (R[:, R_FINAL] - acct.capital_jpy).round(0)
    df["return_pct"] = ((R[:, R_FINAL] / acct.capital_jpy - 1) * 100).round(2)
    df["max_dd_pct"] = (R[:, R_MAXDD] * 100).round(2)
    df["busted"] = R[:, R_BUST].astype(bool)
    df["baskets"] = R[:, R_BASKETS].astype(int)
    df["stops"] = R[:, R_STOPS].astype(int)
    df["max_open"] = R[:, R_MAXPOS].astype(int)
    gl = R[:, R_GROSSL]
    df["pf"] = np.where(gl > 0, R[:, R_GROSSW] / np.where(gl > 0, gl, 1), np.inf).round(2)
    df["min_margin_level_pct"] = np.where(R[:, R_MINML] > 1e17, np.nan, R[:, R_MINML] * 100).round(0)
    df["bust_bar"] = R[:, R_BUSTBAR].astype(int)
    return df


def run_grid(arrays, P: np.ndarray, acct: Account, start: int, end: int) -> pd.DataFrame:
    o, h, l, c, ma, pc, sw, tm = arrays
    R = _run_grid(o, h, l, c, ma, pc, sw, tm, np.ascontiguousarray(P, dtype=np.float64),
                  acct.capital_jpy, acct.usdjpy, acct.leverage, acct.stopout_level,
                  acct.spread_pips, acct.swap_usd_per_lot_day, acct.pip, acct.cooldown_min, start, end)
    return _to_frame(P, R, acct)


def run_one(arrays, p: Params, acct: Account, start: int, end: int, curve: bool = False):
    o, h, l, c, ma, pc, sw, tm = arrays
    eq = np.zeros(end - start) if curve else np.zeros(0)
    r = _simulate(o, h, l, c, ma, pc, sw, tm, p.mode, p.init_lot, p.mult, p.grid_pips, p.max_pos,
                  p.tp_pips, p.stop_pct, p.use_ma, acct.capital_jpy, acct.usdjpy, acct.leverage,
                  acct.stopout_level, acct.spread_pips, acct.swap_usd_per_lot_day, acct.pip,
                  acct.cooldown_min, start, end, eq)
    df = _to_frame(np.array([p.as_row()], dtype=np.float64), r[None, :], acct)
    return (df.iloc[0], eq) if curve else df.iloc[0]
