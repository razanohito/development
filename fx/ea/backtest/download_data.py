"""無料の XAUUSD 1分足サンプル (getdata-finance, MIT ライセンス) を data/ にダウンロードする。

    python download_data.py
"""
import os
import urllib.request

URL = ("https://raw.githubusercontent.com/getdata-finance/"
       "xauusd-1m-ohlcv-metals-historical-data/main/XAUUSD_1m.csv")


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "XAUUSD_1m.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print("downloading", URL)
    urllib.request.urlretrieve(URL, out)
    with open(out) as f:
        n = sum(1 for _ in f) - 1
    print(f"saved {n:,} rows -> {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
