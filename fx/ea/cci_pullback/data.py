"""検証用データの読み込み (data/ 以下, コミットしない)。

data/ が空なら fetch_data.py を先に実行する。
"""
from __future__ import annotations

import functools
import pathlib

import pandas as pd

from cci_bt import resample

DATA = pathlib.Path(__file__).resolve().parent / "data"


@functools.lru_cache(maxsize=None)
def _gold_m1() -> pd.DataFrame:
    df = pd.read_csv(DATA / "XAUUSD_1m.csv.gz")
    df["time"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_localize(None)
    return df[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)


@functools.lru_cache(maxsize=None)
def _fx(pair: str, tf: str) -> pd.DataFrame:
    df = pd.read_csv(DATA / f"{pair}_{tf}.csv", parse_dates=["Date"]).rename(columns={"Date": "time"})
    for col in ("open", "high", "low", "close"):
        df[col] = (df[col] * 1e-5).round(5)
    return (df[["time", "open", "high", "low", "close"]].dropna()
              .drop_duplicates("time").sort_values("time").reset_index(drop=True))


@functools.lru_cache(maxsize=None)
def bars(symbol: str, tf: str) -> pd.DataFrame:
    if symbol == "XAUUSD":
        return resample(_gold_m1(), tf)
    if tf in ("M15", "H1"):
        return _fx(symbol, tf.lower())
    if tf == "H4":
        return resample(_fx(symbol, "h1"), "H4")
    raise ValueError(f"{symbol} {tf} は用意していません")


# 前半 (最適化・比較) / 後半 (検証) の区切り
SPLIT = {
    "XAUUSD": pd.Timestamp("2026-07-06"),
    "EURUSD": pd.Timestamp("2018-01-01"),
    "GBPUSD": pd.Timestamp("2018-01-01"),
}
