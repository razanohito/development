"""検証データ data/XAUUSD_1m.csv.gz を用意する (データ本体はコミットしない)。

XAUUSD 1分足 2026-03-26〜2026-09-25 (getdata-finance 無料サンプル, MIT)。
金ナンピンEAのブランチ (origin/fx-ea-gold-nanpin) にあるファイルを使い、
無ければ GitHub から直接ダウンロードする。

    python3 fetch_data.py
"""
from __future__ import annotations

import gzip
import pathlib
import subprocess
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DST = HERE / "data" / "XAUUSD_1m.csv.gz"
BRANCH_FILE = "origin/fx-ea-gold-nanpin:fx/ea/data/XAUUSD_1m_2026-03-26_2026-09-25.csv.gz"
URL = ("https://raw.githubusercontent.com/getdata-finance/"
       "xauusd-1m-ohlcv-metals-historical-data/main/XAUUSD_1m.csv")


def main() -> None:
    DST.parent.mkdir(exist_ok=True)
    if DST.exists():
        print(f"{DST.name} は用意済み")
        return
    r = subprocess.run(["git", "show", BRANCH_FILE], cwd=HERE, capture_output=True)
    if r.returncode == 0 and r.stdout:
        DST.write_bytes(r.stdout)
    else:
        with urllib.request.urlopen(URL, timeout=300) as resp:
            DST.write_bytes(gzip.compress(resp.read()))
    print(f"{DST.name}: {DST.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
