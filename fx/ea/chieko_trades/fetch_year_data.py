"""1 年テスト (yeartest.py) 用のデータを data/ に用意する (データ本体はコミットしない)。

* data/XAUUSD_1m_long.csv.gz: XAUUSD 1 分足 2026-02-01〜2026-09-25。
  getdata-finance の無料サンプル (MIT) は直近 6 か月だけを毎週差し替えているので、
  リポジトリの最初のコミット (2026-02-01〜07-31) と最新 (2026-03-26〜09-25) をつなぐ。
* data/XAUUSD_m15_ej.csv.gz: XAUUSD 15 分足 2012〜2022-03-04 (ejtraderLabs/historical-data, ライセンス表記なし)。
  1 年通しで取れる金の短期足はこの環境ではこれだけ。

    python3 fetch_year_data.py
"""
from __future__ import annotations

import io
import pathlib
import subprocess
import tempfile
import urllib.request

import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
REPO = "https://github.com/getdata-finance/xauusd-1m-ohlcv-metals-historical-data.git"
FIRST_COMMIT = "74ae763"
EJ_URL = "https://raw.githubusercontent.com/ejtraderLabs/historical-data/main/XAUUSD/XAUUSDm15.csv"


def fetch_m1_long() -> None:
    dst = DATA / "XAUUSD_1m_long.csv.gz"
    if dst.exists():
        print(f"{dst.name} は用意済み")
        return
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo.git"
        subprocess.run(["git", "clone", "-q", "--bare", REPO, str(repo)], check=True)
        parts = []
        for rev in (FIRST_COMMIT, "HEAD"):
            out = subprocess.run(["git", "--git-dir", str(repo), "show", f"{rev}:XAUUSD_1m.csv"],
                                 check=True, capture_output=True).stdout
            parts.append(pd.read_csv(io.BytesIO(out)))
    old, new = parts
    t_old = pd.to_datetime(old["datetime"], utc=True)
    t_new = pd.to_datetime(new["datetime"], utc=True)
    df = pd.concat([old[t_old < t_new.min()], new], ignore_index=True)
    df.to_csv(dst, index=False)
    print(f"{dst.name}: {len(df):,} 行 {df.datetime.iloc[0]} 〜 {df.datetime.iloc[-1]}")


def fetch_m15_ej() -> None:
    dst = DATA / "XAUUSD_m15_ej.csv.gz"
    if dst.exists():
        print(f"{dst.name} は用意済み")
        return
    with urllib.request.urlopen(EJ_URL, timeout=300) as resp:
        df = pd.read_csv(io.BytesIO(resp.read()))
    df.to_csv(dst, index=False)
    print(f"{dst.name}: {len(df):,} 行 {df.iloc[0, 0]} 〜 {df.iloc[-1, 0]}")


def main() -> None:
    DATA.mkdir(exist_ok=True)
    fetch_m1_long()
    fetch_m15_ej()


if __name__ == "__main__":
    main()
