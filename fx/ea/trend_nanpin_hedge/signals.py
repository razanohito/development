"""データ読み込みと、初回エントリーのシグナル (AI の代わりのルール)。

EA と同じく、シグナルは SignalTF (既定 H1) の確定足で判定し、次の足の始値で入る。
  rsi      : 終値が EMA200 より上なら上昇トレンド。RSI(14) が 40 を下から上へ抜けたら買い
             (押し目終わり)。下降トレンドで 60 を上から下へ抜けたら売り。
  breakout : EMA200 の向きに、直近 20 本の高値 (安値) を終値で抜けたら買い (売り)。
  cci      : CCI(期間, 典型価格) が 0 を下から上に抜けたら買い、上から下に抜けたら売り
             (峻さん指定 2026-11-02、ゴールド 5 分足)。ema_filter=True なら H1 EMA200 の向きだけ。
  random   : トレンドを見ずに毎時ランダムな方向で入る (ナンピン・救済の仕組みだけの実力を見る比較用)。
"""
from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"

# 銘柄ごとの前提: pip の大きさ, スプレッド (価格), 契約サイズ
SPECS = {
    "XAUUSD": {"pip": 0.1, "spread": 0.25, "cs": 100.0},
    "EURUSD": {"pip": 0.0001, "spread": 0.00010, "cs": 100000.0},
}


def load(name: str) -> pd.DataFrame:
    if name == "XAUUSD_m1":
        df = pd.read_csv(DATA / "XAUUSD_1m_long.csv.gz")
        df["time"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_localize(None)
        df = df[["time", "open", "high", "low", "close"]]
    elif name in ("XAUUSD_m15", "EURUSD_m15"):
        sym = name.split("_")[0]
        df = pd.read_csv(DATA / f"{sym}_m15_ej.csv.gz")
        scale = 100.0 if sym == "XAUUSD" else 1e5
        df["time"] = pd.to_datetime(df["Date"])
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] / scale
        df = df[["time", "open", "high", "low", "close"]]
    else:
        raise ValueError(name)
    return df.sort_values("time").reset_index(drop=True)


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def cci(hb: pd.DataFrame, n: int) -> pd.Series:
    """MT4/MT5 の iCCI と同じ: (TP - SMA(TP)) / (0.015 × 平均偏差)。TP = (高+安+終)/3。"""
    tp = ((hb["high"] + hb["low"] + hb["close"]) / 3).values
    out = np.full(len(tp), np.nan)
    if len(tp) >= n:
        w = np.lib.stride_tricks.sliding_window_view(tp, n)
        m = w.mean(axis=1)
        md = np.abs(w - m[:, None]).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            out[n - 1:] = (tp[n - 1:] - m) / (0.015 * md)
    return pd.Series(out, index=hb.index)


def signals(df: pd.DataFrame, mode: str, tf: str = "1h", seed: int = 0,
            ema: int = 200, rsi_lo: float = 40, rsi_hi: float = 60, brk: int = 20,
            cci_n: int = 14, ema_filter: bool = False) -> np.ndarray:
    """ベース足ごとのシグナル (+1 買い / -1 売り / 0 なし)。上位足の確定後最初のベース足に置く。"""
    t = df["time"]
    hb = df.set_index("time").resample(tf, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    if mode == "cci":
        r = cci(hb, cci_n)
        buy = (r.shift(1) < 0) & (r >= 0)
        sell = (r.shift(1) > 0) & (r <= 0)
        if ema_filter:
            h1 = df.set_index("time")["close"].resample("1h").last().dropna()
            e = h1.ewm(span=ema, adjust=False).mean()
            # H1 足 [T, T+1h) の確定値は T+1h から使える
            up_h1 = (h1 > e).astype(float)
            up_h1.index = up_h1.index + pd.Timedelta("1h")
            up = up_h1.reindex(hb.index + pd.Timedelta(tf), method="ffill").values == 1.0
            buy = buy & up
            sell = sell & ~up
        hs = pd.Series(0, index=hb.index) + buy.astype(int) - sell.astype(int)
    elif mode == "random":
        rng = np.random.default_rng(seed)
        hs = pd.Series(rng.choice([-1, 1], size=len(hb)), index=hb.index)
    else:
        e = hb["close"].ewm(span=ema, adjust=False).mean()
        up = hb["close"] > e
        if mode == "rsi":
            r = _rsi(hb["close"])
            buy = up & (r.shift(1) < rsi_lo) & (r >= rsi_lo)
            sell = (~up) & (r.shift(1) > rsi_hi) & (r <= rsi_hi)
        elif mode == "breakout":
            hh = hb["high"].rolling(brk).max().shift(1)
            ll = hb["low"].rolling(brk).min().shift(1)
            buy = up & (hb["close"] > hh)
            sell = (~up) & (hb["close"] < ll)
        else:
            raise ValueError(mode)
        hs = pd.Series(0, index=hb.index) + buy.astype(int) - sell.astype(int)
        hs.iloc[:ema] = 0
    # 上位足 [T, T+tf) が確定 → T+tf 以降最初のベース足でエントリー
    close_time = hs.index + pd.Timedelta(tf)
    idx = np.searchsorted(t.values, close_time.values)
    out = np.zeros(len(df), np.int64)
    ok = (idx < len(df)) & (hs.values != 0)
    out[idx[ok]] = hs.values[ok]
    return out


def daily_atr_pips(df: pd.DataFrame, pip: float, n: int = 14) -> np.ndarray:
    """各ベース足時点で使える日足 ATR(n) (前日までの確定足, pips)。EA の iATR(PERIOD_D1, 14) の shift 1 に相当。"""
    d = df.set_index("time").resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(n).mean().shift(1)  # 当日はまだ確定していないので前日まで
    day = df["time"].dt.floor("D")
    v = atr.reindex(day.values).values / pip
    v = pd.Series(v).bfill().values  # 最初の n 日は最初の値で埋める
    return v


def adx_block(df: pd.DataFrame, thr: float, tf: str = "1h", n: int = 14) -> np.ndarray:
    """強いトレンドのときは入らないためのマスク。確定 H1 足の ADX(n) > thr なら True (= 入らない)。
    ADX は Wilder 平滑 (MT4/MT5 の iADX は指数平滑なので値は少しずれる)。"""
    hb = df.set_index("time").resample(tf).agg({"high": "max", "low": "min", "close": "last"}).dropna()
    up = hb["high"].diff()
    dn = -hb["low"].diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    pc = hb["close"].shift(1)
    tr = pd.concat([hb["high"] - hb["low"], (hb["high"] - pc).abs(), (hb["low"] - pc).abs()], axis=1).max(axis=1)
    a = 1.0 / n
    atr = tr.ewm(alpha=a, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, index=hb.index).ewm(alpha=a, adjust=False).mean() / atr
    ndi = 100 * pd.Series(ndm, index=hb.index).ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    adx = dx.ewm(alpha=a, adjust=False).mean()
    adx.index = adx.index + pd.Timedelta(tf)  # 確定後から使える
    v = adx.reindex(df["time"].values, method="ffill").values
    return np.nan_to_num(v, nan=0.0) > thr
