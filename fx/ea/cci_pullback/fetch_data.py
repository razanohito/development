"""検証データを data/ に用意する (データ本体はコミットしない)。

- XAUUSD 1分足 2026-03-26〜2026-09-25 (getdata-finance 無料サンプル, MIT):
    金ナンピンEA (fx/ea/data/) と同じファイル。リポジトリ内に無ければ
    origin/fx-ea-gold-nanpin ブランチから取り出す。
- EURUSD / GBPUSD 15分足・1時間足 2012-11〜2022-03:
    https://github.com/ejtraderLabs/historical-data (価格は 1e5 倍の整数表記)

使い方:
    python3 fetch_data.py
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
GOLD_NAME = "XAUUSD_1m_2026-03-26_2026-09-25.csv.gz"
FX_URL = "https://raw.githubusercontent.com/ejtraderLabs/historical-data/main/{p}/{p}{tf}.csv"


def fetch_gold() -> None:
    dst = DATA / "XAUUSD_1m.csv.gz"
    if dst.exists():
        return
    local = HERE.parent / "data" / GOLD_NAME
    if local.exists():
        shutil.copy(local, dst)
        return
    blob = subprocess.run(
        ["git", "show", f"origin/fx-ea-gold-nanpin:fx/ea/data/{GOLD_NAME}"],
        cwd=HERE, check=True, capture_output=True).stdout
    dst.write_bytes(blob)


def fetch_fx() -> None:
    for pair in ("EURUSD", "GBPUSD"):
        for tf in ("m15", "h1"):
            dst = DATA / f"{pair}_{tf}.csv"
            if dst.exists():
                continue
            with urllib.request.urlopen(FX_URL.format(p=pair, tf=tf), timeout=300) as r:
                dst.write_bytes(r.read())


def main() -> None:
    DATA.mkdir(exist_ok=True)
    fetch_gold()
    fetch_fx()
    for f in sorted(DATA.iterdir()):
        print(f"{f.name}: {f.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
