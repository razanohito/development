"""CCI 押し目買い (風林火山) EA のバックテストエンジン。

ルール (CCIPullbackEA.mq5 と同じ):
  ① 環境認識 (上位足, 確定足のみ使用)
     - 上昇トレンド: 直近2つのスイング高値・安値がともに切り上がり、終値が直近の押し安値より上
     - 上位足 CCI >= env_cci
  ② トリガー (下位足, 確定足で判定し次の足の始値でエントリー)
     - CCI が -100 以下から -100 を上抜けた
     - 直近 lookback 本の CCI 最安値が -max_depth より上 (深すぎる下落は除外, 0 で無効)
     - (任意) 陽線包み足 or 下ヒゲ反発
     - (任意) 押しの安値が 20EMA 付近まで来ていた
     - (任意) 押しが上位足の直近上昇波のフィボ 38.2%〜78.6% に収まる
  ③ 決済
     - 損切: CCI が再び -100 割れ + 陰線確定 → 次の足の始値で決済
     - 緊急損切 (任意): 押し安値 - 0.2*ATR に逆指値
     - 利確 (tp_mode, いずれも「CCI が +100 以上に到達した後」に判定):
         1 cci_back  : CCI が +100 を下回った (勢い喪失)
         2 cci_peak  : CCI が前の足より下がった (ピークアウト)
         3 adx_fall  : ADX が前の足より下がった (トレンドの強さが低下)
         4 range     : 直近 range_n 本の高安幅 < range_k * ATR (値幅が縮みレンジ化)
         5 diverge   : 高値更新したのに CCI がピーク未満 (ダイバージェンス) か CCI < 0
         6 cci_zero  : CCI が 0 を下回った (長く引っ張る)
         7 fixed_2R  : 緊急損切幅の2倍で指値 (比較用, 画像のルールにはない)
         8 angle     : +100 到達時の CCI 上昇角度が急なら cci_peak, 緩やかなら cci_zero
         0 none      : 利確なし (損切ルールのみ, 比較用)
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field

import numpy as np
import pandas as pd
from numba import njit

TP_MODES = {
    "none": 0, "cci_back": 1, "cci_peak": 2, "adx_fall": 3, "range": 4,
    "diverge": 5, "cci_zero": 6, "fixed_2R": 7, "angle": 8,
}
TP_LABEL = {
    "none": "利確なし (損切ルールのみ)",
    "cci_back": "CCIが+100を割り込む",
    "cci_peak": "CCIがピークアウト (前の足より低下)",
    "adx_fall": "ADXが低下",
    "range": "値幅が縮小 (直近6本の高安幅 < 2ATR)",
    "diverge": "ダイバージェンス or CCI<0",
    "cci_zero": "CCIが0を割り込む",
    "fixed_2R": "固定 2R (比較用)",
    "angle": "CCI角度で切替 (急→ピークアウト / 緩→0割れ)",
}

TF_MIN = {"M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240}


# ---------------------------------------------------------------- symbols
@dataclass(frozen=True)
class Symbol:
    name: str
    usd_per_price_per_lot: float  # 1 lot で価格が 1 動いた時の損益 (USD)
    spread: float                 # 価格単位
    swap_long_per_lot_day: float  # USD / lot / 日


SYMBOLS = {
    # 1 lot = 100 oz, スプレッド $0.30, 買いスワップ -$50/lot/日 (金ナンピン検証と同じ)
    "XAUUSD": Symbol("XAUUSD", 100.0, 0.30, -50.0),
    # 1 lot = 100,000 通貨, スプレッド 0.5pips, 買いスワップ -$5/lot/日 (EURUSD 検証と同じ)
    "EURUSD": Symbol("EURUSD", 100_000.0, 0.00005, -5.0),
    "GBPUSD": Symbol("GBPUSD", 100_000.0, 0.00007, -5.0),
}
USDJPY = 150.0
CAPITAL_JPY = 5_000_000.0


# ---------------------------------------------------------------- indicators (MT5 準拠)
def sma(x: np.ndarray, n: int) -> np.ndarray:
    s = pd.Series(x).rolling(n, min_periods=n).mean()
    return s.to_numpy()


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """MT5 の ExponentialMA と同じ (先頭値から開始)。"""
    a = 2.0 / (n + 1.0)
    out = np.empty_like(x, dtype=float)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * a + out[i - 1] * (1 - a)
    return out


@njit(cache=True)
def _cci(tp: np.ndarray, n: int) -> np.ndarray:
    out = np.full(tp.shape[0], np.nan)
    for i in range(n - 1, tp.shape[0]):
        m = 0.0
        for k in range(i - n + 1, i + 1):
            m += tp[k]
        m /= n
        d = 0.0
        for k in range(i - n + 1, i + 1):
            d += abs(tp[k] - m)
        d = d * 0.015 / n
        out[i] = (tp[i] - m) / d if d != 0 else 0.0
    return out


def cci(h, l, c, n: int = 14) -> np.ndarray:
    """MT5 iCCI(PRICE_TYPICAL)。"""
    return _cci((np.asarray(h) + np.asarray(l) + np.asarray(c)) / 3.0, n)


def true_range(h, l, c) -> np.ndarray:
    pc = np.r_[c[0], c[:-1]]
    return np.maximum(h, pc) - np.minimum(l, pc)


def atr(h, l, c, n: int = 14) -> np.ndarray:
    """MT5 iATR (TR の単純移動平均)。"""
    return sma(true_range(h, l, c), n)


def adx(h, l, c, n: int = 14) -> np.ndarray:
    """MT5 iADX (ADX.mq5) と同じ計算。"""
    ph = np.r_[h[0], h[:-1]]
    pl = np.r_[l[0], l[:-1]]
    pos = np.maximum(h - ph, 0.0)
    neg = np.maximum(pl - l, 0.0)
    pos2 = np.where(pos > neg, pos, 0.0)
    neg2 = np.where(neg > pos, neg, 0.0)
    tr = true_range(h, l, c)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi_raw = np.where(tr != 0, 100.0 * pos2 / tr, 0.0)
        ndi_raw = np.where(tr != 0, 100.0 * neg2 / tr, 0.0)
    pdi = ema(pdi_raw, n)
    ndi = ema(ndi_raw, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(pdi + ndi != 0, 100.0 * np.abs(pdi - ndi) / (pdi + ndi), 0.0)
    return ema(dx, n)


# ---------------------------------------------------------------- 上位足の環境認識
def swing_structure(h: np.ndarray, l: np.ndarray, c: np.ndarray, k: int = 2):
    """各上位足 t (確定) 時点での直近スイング情報を返す。

    スイング高値 i: high[i] > max(high[i-k..i-1]) かつ high[i] >= max(high[i+1..i+k])
    (右側 k 本が確定した t = i+k 以降で初めて使える)。安値も同様。
    戻り値: up (bool), last_sl (直近押し安値), last_sh (直近高値)
    """
    n = len(h)
    up = np.zeros(n, dtype=bool)
    last_sl = np.full(n, np.nan)
    last_sh = np.full(n, np.nan)
    sh = [np.nan, np.nan]
    sl = [np.nan, np.nan]
    for t in range(n):
        i = t - k
        if i - k >= 0:
            if h[i] > h[i - k:i].max() and h[i] >= h[i + 1:t + 1].max():
                sh = [sh[1], h[i]]
            if l[i] < l[i - k:i].min() and l[i] <= l[i + 1:t + 1].min():
                sl = [sl[1], l[i]]
        last_sl[t] = sl[1]
        last_sh[t] = sh[1]
        if not np.isnan(sh[0]) and not np.isnan(sl[0]):
            up[t] = sh[1] > sh[0] and sl[1] > sl[0] and c[t] > sl[1]
    return up, last_sl, last_sh


def htf_features(htf: pd.DataFrame, tf_min: int, env_ma: int = 50, swing_k: int = 2) -> pd.DataFrame:
    h, l, c = (htf[x].to_numpy() for x in ("high", "low", "close"))
    up, last_sl, last_sh = swing_structure(h, l, c, swing_k)
    e = ema(c, env_ma)
    f = pd.DataFrame({
        "h_up": up,
        "h_sl": last_sl,
        "h_sh": last_sh,
        "h_cci": cci(h, l, c, 14),
        "h_ema_up": (c > e) & (e > np.r_[e[:3], e[:-3]]),
    })
    # 上位足は確定してから使う: 利用可能時刻 = 足の開始 + 足の長さ
    f["avail"] = htf["time"].to_numpy() + pd.Timedelta(minutes=tf_min)
    return f


# ---------------------------------------------------------------- データ
def resample(m1: pd.DataFrame, tf: str) -> pd.DataFrame:
    r = (m1.set_index("time")
           .resample(f"{TF_MIN[tf]}min", label="left", closed="left")
           .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
           .dropna()
           .reset_index())
    return r


@dataclass
class Params:
    ltf: str = "M5"
    htf: str = "H1"
    env: str = "structure"     # structure / ema / none
    env_cci: float = 50.0      # 上位足 CCI の下限 (-1e9 で無効)
    trig_level: float = -100.0
    max_depth: float = 0.0     # 0=無効, 例 200 → 直近の CCI 最安値が -200 より深ければ見送り
    depth_lookback: int = 20
    candle: int = 0            # 1 = 陽線包み足 or 下ヒゲ反発を要求
    ema_touch: int = 0         # 1 = 押しの安値が 20EMA ± 0.5ATR に触れていること
    fib: int = 0               # 1 = 押しが上位足上昇波の 38.2〜78.6%
    stop_rule: int = 1         # 1 = CCI -100 割れ + 陰線で撤退
    hard_sl: int = 1           # 1 = 押し安値 - 0.2ATR に逆指値
    sl_lookback: int = 10
    tp_mode: str = "cci_back"
    tp_level: float = 100.0
    range_n: int = 6
    range_k: float = 2.0
    angle_th: float = 150.0    # +100 到達時、3本前からの CCI 上昇幅がこれ以上なら「急」
    max_bars: int = 0          # 0=無効
    lot: float = 0.01

    def key(self) -> str:
        return "|".join(f"{k}={v}" for k, v in asdict(self).items())


def build_frame(ltf: pd.DataFrame, htf: pd.DataFrame, p: Params) -> pd.DataFrame:
    """下位足に指標と上位足 (確定足) の情報を結合したフレーム。"""
    d = ltf.copy()
    h, l, c, o = (d[x].to_numpy() for x in ("high", "low", "close", "open"))
    d["cci"] = cci(h, l, c, 14)
    d["ema20"] = ema(c, 20)
    d["atr"] = atr(h, l, c, 14)
    d["adx"] = adx(h, l, c, 14)
    d["close_time"] = d["time"] + pd.Timedelta(minutes=TF_MIN[p.ltf])
    hf = htf_features(htf, TF_MIN[p.htf])
    d = pd.merge_asof(d.sort_values("close_time"), hf.sort_values("avail"),
                      left_on="close_time", right_on="avail", direction="backward")
    return d


def entry_signal(d: pd.DataFrame, p: Params) -> tuple[np.ndarray, np.ndarray]:
    """確定足 i で買いシグナルが出たか、とその時の緊急損切価格。"""
    o, h, l, c = (d[x].to_numpy() for x in ("open", "high", "low", "close"))
    cc = d["cci"].to_numpy()
    prev = np.r_[np.nan, cc[:-1]]
    sig = (prev <= p.trig_level) & (cc > p.trig_level)

    if p.env == "structure":
        sig &= d["h_up"].fillna(False).to_numpy(bool)
    elif p.env == "ema":
        sig &= d["h_ema_up"].fillna(False).to_numpy(bool)
    if p.env_cci > -1e8:
        sig &= d["h_cci"].to_numpy() >= p.env_cci

    if p.max_depth > 0:
        mn = pd.Series(cc).rolling(p.depth_lookback, min_periods=1).min().to_numpy()
        sig &= mn >= -p.max_depth

    a = d["atr"].to_numpy()
    if p.candle:
        po, pc = np.r_[np.nan, o[:-1]], np.r_[np.nan, c[:-1]]
        engulf = (c > o) & (pc < po) & (c >= po) & (o <= pc)
        rng = h - l
        wick = (np.minimum(o, c) - l) >= 0.5 * rng
        pat = engulf | wick
        pat_prev = np.r_[False, pat[:-1]]
        sig &= pat | pat_prev

    low_min = pd.Series(l).rolling(p.sl_lookback, min_periods=1).min().to_numpy()
    if p.ema_touch:
        e = d["ema20"].to_numpy()
        sig &= (low_min <= e + 0.5 * a)
    if p.fib:
        hs, hl = d["h_sh"].to_numpy(), d["h_sl"].to_numpy()
        wave = hs - hl
        with np.errstate(invalid="ignore", divide="ignore"):
            ret = (hs - low_min) / wave
        sig &= (wave > 0) & (ret >= 0.382) & (ret <= 0.786)

    sl = low_min - 0.2 * a
    sig &= ~np.isnan(a) & ~np.isnan(cc)
    return sig, sl


# ---------------------------------------------------------------- 売買シミュレーション
@njit(cache=True)
def _simulate(o, h, l, c, cc, adxv, atrv, sig, slp, spread,
              stop_rule, hard_sl, tp_mode, tp_level, range_n, range_k, angle_th, max_bars):
    n = o.shape[0]
    ent_i = np.empty(n, np.int64)
    ex_i = np.empty(n, np.int64)
    ent_p = np.empty(n)
    ex_p = np.empty(n)
    reason = np.empty(n, np.int64)  # 0=損切ルール 1=利確 2=緊急損切 3=指値 4=時間 5=データ終了
    nt = 0
    i = 0
    while i < n - 1:
        if not sig[i]:
            i += 1
            continue
        e = i + 1
        entry = o[e] + spread           # ask で買う
        sl = slp[i] if hard_sl == 1 else -1.0
        if sl > 0 and o[e] <= sl:
            i += 1
            continue
        tp = -1.0
        if tp_mode == 7:
            if sl <= 0:
                i += 1
                continue
            tp = entry + 2.0 * (entry - sl)
        reached = False
        peak = -1e18
        hh = h[e]
        fast = False
        exit_j = -1
        exit_p = 0.0
        why = 5
        j = e
        while j < n:
            # 足の途中: 逆指値 → 指値 (同じ足ならより不利な逆指値を優先)
            if sl > 0 and l[j] <= sl:
                exit_j = j
                exit_p = min(o[j], sl)
                why = 2
                break
            if tp > 0 and h[j] >= tp:
                exit_j = j
                exit_p = max(o[j], tp)
                why = 3
                break
            if j == n - 1:
                exit_j = j
                exit_p = c[j]
                why = 5
                break
            # 足の確定で判定 → 次の足の始値で決済
            x = cc[j]
            new_high = h[j] > hh and j > e
            if x >= tp_level and not reached:
                reached = True
                fast = j >= 3 and (x - cc[j - 3]) >= angle_th
            out = False
            if stop_rule == 1 and x < -100.0 and c[j] < o[j]:
                out = True
                why = 0
            elif reached and j > e:
                if tp_mode == 1:
                    out = x < tp_level
                elif tp_mode == 2:
                    out = x < cc[j - 1]
                elif tp_mode == 3:
                    out = adxv[j] < adxv[j - 1]
                elif tp_mode == 4:
                    if j - range_n + 1 >= 0:
                        hi = h[j - range_n + 1:j + 1].max()
                        lo = l[j - range_n + 1:j + 1].min()
                        out = (hi - lo) < range_k * atrv[j]
                elif tp_mode == 5:
                    out = (new_high and x < peak) or x < 0.0
                elif tp_mode == 6:
                    out = x < 0.0
                elif tp_mode == 8:
                    out = (x < cc[j - 1]) if fast else (x < 0.0)
                if out:
                    why = 1
            if not out and max_bars > 0 and j - e + 1 >= max_bars:
                out = True
                why = 4
            if reached and x > peak:
                peak = x
            if h[j] > hh:
                hh = h[j]
            if out:
                exit_j = j + 1
                exit_p = o[j + 1]
                break
            j += 1
        ent_i[nt] = e
        ex_i[nt] = exit_j
        ent_p[nt] = entry
        ex_p[nt] = exit_p
        reason[nt] = why
        nt += 1
        # 決済した足の確定以降に次のシグナルを探す
        i = exit_j if why in (0, 1, 4) else exit_j
        if i <= e:
            i = e
    return ent_i[:nt], ex_i[:nt], ent_p[:nt], ex_p[:nt], reason[:nt]


REASON_NAME = {0: "損切ルール", 1: "利確", 2: "緊急損切", 3: "指値", 4: "時間", 5: "期間終了"}


def run(d: pd.DataFrame, p: Params, sym: Symbol) -> pd.DataFrame:
    sig, slp = entry_signal(d, p)
    arr = lambda k: d[k].to_numpy(float)
    ei, xi, ep, xp, rs = _simulate(
        arr("open"), arr("high"), arr("low"), arr("close"), arr("cci"), arr("adx"), arr("atr"),
        sig, slp, sym.spread, p.stop_rule, p.hard_sl, TP_MODES[p.tp_mode], p.tp_level,
        p.range_n, p.range_k, p.angle_th, p.max_bars)
    t = d["time"].to_numpy()
    tr = pd.DataFrame({
        "entry_time": t[ei], "exit_time": t[xi], "entry": ep, "exit": xp,
        "reason": [REASON_NAME[r] for r in rs], "bars": xi - ei,
    })
    days = (tr["exit_time"].dt.normalize() - tr["entry_time"].dt.normalize()).dt.days
    tr["swap_usd"] = days * sym.swap_long_per_lot_day * p.lot
    tr["pnl_usd"] = (tr["exit"] - tr["entry"]) * sym.usd_per_price_per_lot * p.lot + tr["swap_usd"]
    tr["pnl_jpy"] = tr["pnl_usd"] * USDJPY
    return tr


def metrics(tr: pd.DataFrame, lot: float = 0.01) -> dict:
    if len(tr) == 0:
        return dict(trades=0, win_rate=np.nan, pf=np.nan, net_jpy=0.0, max_dd_jpy=0.0,
                    avg_win_jpy=np.nan, avg_loss_jpy=np.nan, avg_bars=np.nan)
    p = tr["pnl_jpy"].to_numpy()
    eq = np.cumsum(p)
    dd = np.max(np.maximum.accumulate(np.r_[0.0, eq]) - np.r_[0.0, eq])
    w, ls = p[p > 0], p[p <= 0]
    return dict(
        trades=len(p),
        win_rate=len(w) / len(p) * 100,
        pf=(w.sum() / -ls.sum()) if ls.sum() < 0 else np.inf,
        net_jpy=p.sum(),
        max_dd_jpy=dd,
        avg_win_jpy=w.mean() if len(w) else 0.0,
        avg_loss_jpy=ls.mean() if len(ls) else 0.0,
        avg_bars=tr["bars"].mean(),
    )
