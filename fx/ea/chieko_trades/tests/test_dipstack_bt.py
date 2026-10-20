"""dipstack_bt.run の手計算テスト (データ不要)。  python3 -m pytest tests"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import dipstack_bt as bt  # noqa: E402


def _run(o, h, l, c, *, dip=5.0, step=10.0, max_levels=3, tp=5.0, sl=0.0, spread=0.0, swap=0.0,
         rollover=None):
    n = len(o)
    o, h, l, c = (np.asarray(x, dtype=float) for x in (o, h, l, c))
    roll_high = np.full(n, 100.0)
    trend = np.ones(n)
    rollover = np.zeros(n, dtype=np.bool_) if rollover is None else np.asarray(rollover, dtype=np.bool_)
    weekday = np.zeros(n, dtype=np.int64)
    return bt.run(o, h, l, c, rollover, weekday, roll_high, trend, 1.0, 1, dip, step, max_levels, tp, sl, 0,
                  spread, swap, 0, n)


def test_single_level_take_profit():
    # 95 で買い (高値 100 - 5)、100 に届いて +5 x 100 オンス
    pnl, dd, maxlots, _, span = _run([95, 96], [96, 100.5], [94, 95.5], [96, 100])
    assert np.allclose(pnl, [500.0])
    assert maxlots == 1.0
    assert tuple(span[0]) == (0, 1)


def test_average_down_then_basket_tp():
    # 95 で買い → 85 で追加 → 平均 90 → 95 で 2 ロット x $5
    pnl, dd, maxlots, _, _ = _run([95, 90, 88], [95, 90, 95.5], [95, 84, 87], [95, 86, 95])
    assert np.allclose(pnl, [1000.0])
    assert maxlots == 2.0
    # 最大DD: 2 本目の足の安値 84 で (84-95)+(84-85) = -12 ドル x 100
    assert np.isclose(dd, 1200.0)


def test_stop_loss_and_spread():
    # 買値 95 + スプレッド 1 = 96、損切り $10 → Bid 86 で -10 x 100
    pnl, *_ = _run([95, 90], [95, 90], [95, 80], [95, 82], sl=10.0, max_levels=1, spread=1.0)
    assert np.allclose(pnl, [-1000.0])


def test_swap_charged_on_rollover():
    pnl, *_ = _run([95, 95, 95], [95, 95, 100.5], [95, 95, 95], [95, 95, 100],
                   swap=30.0, rollover=[False, True, False])
    assert np.allclose(pnl, [500.0 - 30.0])


def test_open_basket_closed_at_end():
    pnl, *_ = _run([95, 93], [95, 93], [95, 92], [95, 92])
    assert np.allclose(pnl, [-300.0])
