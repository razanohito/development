"""合成データでバックテスターの基本動作を確認する。  python3 -m pytest tests -q"""
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import nanpin_bt as bt  # noqa: E402

ACCT = bt.Account(spread_pips=0.0, swap_usd_per_lot_day=0.0)


def bars(closes, start="2020-01-06"):
    c = np.asarray(closes, dtype=float)
    o = np.r_[c[0], c[:-1]]
    t = pd.date_range(start, periods=len(c), freq="h")
    return pd.DataFrame({"time": t, "open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c})


def run(df, **kw):
    p = bt.Params(**kw)
    return bt.run_one(bt.prepare(df), p, ACCT, 0, len(df))


def test_single_take_profit():
    # 1.1000 で買い → 1.1010 で 10pips 利確: 0.1 ロット = 10 USD = 1500 円
    df = bars([1.1000, 1.1000, 1.1005, 1.1010, 1.1010])
    r = run(df, mode=bt.MODE_BUY, init_lot=0.1, grid_pips=20, max_pos=5, tp_pips=10)
    assert r.baskets >= 1
    # 利確後すぐ次のバスケットを建てて期末は同値なので損益は利確1回分
    assert abs(r.profit_jpy - 1500) < 1


def test_nanpin_basket_take_profit():
    # 1.1000 買い 0.1、1.0980 で 0.1 追加 (倍率1.0)。目標利益は「初期 0.1 ロットが 10pips 動いた分」=10 USD。
    # 合計 0.2 ロットなので平均 1.0990 + 5pips = 1.0995 で一括決済される
    df = bars([1.1000, 1.0990, 1.0980, 1.0990, 1.1000])
    r = run(df, mode=bt.MODE_BUY, init_lot=0.1, mult=1.0, grid_pips=20, max_pos=5, tp_pips=10)
    assert r.max_open == 2
    assert r.baskets == 1
    assert abs(r.profit_jpy - 1500) < 1


def test_lot_multiplier_rounding():
    assert bt._lot(0.01, 1.2, 0) == 0.01
    assert bt._lot(0.01, 1.2, 3) == 0.02  # 0.01728 → 0.02
    assert abs(bt._lot(0.1, 1.5, 2) - 0.23) < 1e-9  # 0.225 → 0.23 (四捨五入)


def test_downtrend_busts_buy_only():
    df = bars(np.linspace(1.20, 0.80, 2000))
    r = run(df, mode=bt.MODE_BUY, init_lot=0.1, mult=1.5, grid_pips=10, max_pos=20, tp_pips=10)
    assert r.busted


def test_account_stop_limits_loss():
    df = bars(np.linspace(1.20, 1.00, 2000))
    r = run(df, mode=bt.MODE_BUY, init_lot=0.01, mult=1.2, grid_pips=20, max_pos=20, tp_pips=10, stop_pct=10)
    assert not r.busted
    assert r.stops >= 1
    # 一回の損切りは残高の 10% ちょうどで止まる
    assert r.max_dd_pct < 10.0 * r.stops + 1


def test_ma_filter_blocks_counter_trend_entries():
    # 下落トレンド中は 4時間足 100SMA の下なので買いバスケットは始まらない
    df = bars(np.linspace(1.20, 1.10, 600))
    r = run(df, mode=bt.MODE_BUY, init_lot=0.1, grid_pips=20, max_pos=5, tp_pips=10, use_ma=1)
    assert r.max_open == 0
    assert r.profit_jpy == 0


def test_cooldown_after_account_stop():
    # 急落で口座損切り → 1440 分は新規を建てない。その後は再開する
    closes = np.r_[np.linspace(1.20, 1.10, 200), np.full(23, 1.10), np.full(10, 1.10)]
    df = bars(closes)
    r = run(df, mode=bt.MODE_BUY, init_lot=0.1, mult=1.0, grid_pips=20, max_pos=50, tp_pips=10, stop_pct=1)
    assert r.stops >= 1
    no_cool = bt.run_one(bt.prepare(df), bt.Params(mode=bt.MODE_BUY, init_lot=0.1, mult=1.0, grid_pips=20,
                                                  max_pos=50, tp_pips=10, stop_pct=1),
                         bt.Account(spread_pips=0.0, swap_usd_per_lot_day=0.0, cooldown_min=0), 0, len(df))
    assert no_cool.stops > r.stops
