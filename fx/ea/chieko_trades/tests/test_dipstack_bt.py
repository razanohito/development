"""dipstack_bt.run の手計算テスト (データ不要)。  python3 -m pytest tests"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import dipstack_bt as bt  # noqa: E402


def _run(o, h, l, c, *, dip=5.0, step=10.0, max_levels=3, tp=5.0, sl=0.0, spread=0.0, swap=0.0,
         rollover=None, max_hold=0, cooldown=0, trend=None, tmin=None):
    n = len(o)
    o, h, l, c = (np.asarray(x, dtype=float) for x in (o, h, l, c))
    roll_high = np.full(n, 100.0)
    trend = np.ones(n) if trend is None else np.asarray(trend, dtype=float)
    tmin = np.arange(n, dtype=np.int64) if tmin is None else np.asarray(tmin, dtype=np.int64)
    rollover = np.zeros(n, dtype=np.bool_) if rollover is None else np.asarray(rollover, dtype=np.bool_)
    weekday = np.zeros(n, dtype=np.int64)
    return bt.run(o, h, l, c, rollover, weekday, roll_high, trend, 1.0, 1, dip, step, max_levels, tp, sl, 1,
                  spread, swap, 0, n, tmin, max_hold, cooldown)


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


def test_max_hold_closes_at_bar_close():
    # 95 で買い、2 分後の足の終値 93 で時間切れ決済 → -2 x 100
    pnl, *_ , span = _run([95, 94, 93, 99], [95, 94, 93, 99], [95, 93, 92, 93], [95, 94, 93, 99], max_hold=2)
    assert np.allclose(pnl[0], -200.0)
    assert tuple(span[0]) == (0, 2)


def test_cooldown_blocks_reentry_after_stop():
    # 1 本目で買って損切り → クールダウン 10 分の間は押し目でも買わない
    o = [95, 90, 95, 95, 95]
    pnl, *_ = _run(o, o, [95, 80, 95, 95, 95], o, sl=10.0, max_levels=1, cooldown=10)
    assert np.allclose(pnl, [-1000.0])
    pnl, *_ = _run(o, o, [95, 80, 95, 95, 95], o, sl=10.0, max_levels=1, cooldown=0)
    assert len(pnl) == 2


def test_trend_filter_blocks_entry():
    pnl, *_ = _run([95, 96], [96, 100.5], [94, 95.5], [96, 100], trend=[-1.0, -1.0])
    assert len(pnl) == 0
