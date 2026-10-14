"""EURUSD / GBPUSD の過去データを取得して data/ に保存する。

- 1時間足・15分足 (2012-11 〜 2022-03, OHLC):
    https://github.com/ejtraderLabs/historical-data
    価格は 1e5 倍の整数表記なので 1e-5 を掛けて通常の価格に戻す。
- 日足終値 (1971 〜 最新, FRED H.10 の正午NY レート):
    https://github.com/datasets/exchange-rates (PDDL)
    「1ドル=何ユーロ/ポンド」表記なので逆数を取って EURUSD / GBPUSD にする。

データ本体はリポジトリにコミットしない (data/ は .gitignore 済み)。

使い方:
    python3 fetch_data.py
"""
from __future__ import annotations

import io
import pathlib
import urllib.request

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"

H1_URL = "https://raw.githubusercontent.com/ejtraderLabs/historical-data/main/{p}/{p}{tf}.csv"
DAILY_URL = "https://raw.githubusercontent.com/datasets/exchange-rates/main/data/daily.csv"
DAILY_COUNTRY = {"EURUSD": "Euro", "GBPUSD": "United Kingdom"}
PAIRS = ("EURUSD", "GBPUSD")


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def fetch_h1(pair: str, tf: str = "h1") -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(_get(H1_URL.format(p=pair, tf=tf))), parse_dates=["Date"])
    df = df.rename(columns={"Date": "time"})
    for col in ("open", "high", "low", "close"):
        df[col] = (df[col] * 1e-5).round(5)
    df = df[["time", "open", "high", "low", "close"]].dropna()
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


def fetch_daily() -> dict[str, pd.DataFrame]:
    raw = pd.read_csv(io.BytesIO(_get(DAILY_URL)), parse_dates=["Date"])
    out = {}
    for pair, country in DAILY_COUNTRY.items():
        d = raw[(raw["Country"] == country) & (raw["Exchange rate"] > 0)].copy()
        d["close"] = (1.0 / d["Exchange rate"]).round(5)
        d = d.rename(columns={"Date": "time"})[["time", "close"]]
        out[pair] = d.sort_values("time").reset_index(drop=True)
    return out


def main() -> None:
    DATA.mkdir(exist_ok=True)
    for pair in PAIRS:
        df = fetch_h1(pair)
        path = DATA / f"{pair}_H1.csv"
        df.to_csv(path, index=False)
        print(f"{path.name}: {len(df)} bars {df.time.iloc[0]} .. {df.time.iloc[-1]}")
        # 15分足は「1時間足のバー内経路の仮定」が結果を歪めていないかの確認用
        df = fetch_h1(pair, "m15")
        path = DATA / f"{pair}_M15.csv"
        df.to_csv(path, index=False)
        print(f"{path.name}: {len(df)} bars {df.time.iloc[0]} .. {df.time.iloc[-1]}")
    for pair, df in fetch_daily().items():
        path = DATA / f"{pair}_D1_close.csv"
        df.to_csv(path, index=False)
        print(f"{path.name}: {len(df)} days {df.time.iloc[0].date()} .. {df.time.iloc[-1].date()}")


if __name__ == "__main__":
    main()
