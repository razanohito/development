"""tnh_bt.simulate を手計算できる値動きで確かめる。 python3 -m pytest tests"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tnh_bt import simulate  # noqa: E402


def run(path, sig0=1, spread=0.0, **kw):
    """path: 終値の列。各バーは 始値=前の終値, 高値/安値=両端 の 1 区間として扱う。"""
    p = np.array(path, float)
    o = np.r_[p[0], p[:-1]]
    c = p
    h = np.maximum(o, c)
    l = np.minimum(o, c)
    sig = np.zeros(len(p), np.int64)
    sig[0] = sig0
    a = dict(lot0=1.0, mult=1.5, max_pos=5, step=10.0, hedge_from=3, after_hedge=0,
             htp_r=1.0, tp_r=0.5, rev_from=4, rev_r=0.3, max_loss_money=0.0, lot_step=0.5)
    a.update(kw)
    step_arr = np.full(len(p), a["step"])
    return simulate(o, h, l, c, sig, 1.0, spread, 1.0, a["lot0"], a["mult"], a["max_pos"], step_arr,
                    a["hedge_from"], a["after_hedge"], a["htp_r"], a["tp_r"], a["rev_from"],
                    a["rev_r"], a["max_loss_money"], a["lot_step"])


def test_basket_tp_with_one_position():
    eq, cyc = run([100, 103, 110])
    assert len(cyc) == 1
    assert cyc[0, 0] == 5.0 and cyc[0, 1] == 1  # 105 で +5 決済


def test_second_position_and_basket_tp():
    # 100 で買い1、90 で買い1.5(→1.5)。平均 94、+5 は 2.5 ロット合計 → 96
    eq, cyc = run([100, 90, 99])
    assert cyc[0, 1] == 2
    assert abs(cyc[0, 0] - 5.0) < 1e-9


def test_hedge_take_profit_then_recover():
    # 100 買1, 90 買1.5, 80 売2.25→2.5(0.5刻み), 75 で売り利確 +12.5
    # その後 上昇: (p-100)*1 + (p-90)*1.5 + 12.5 = 5 → p=91
    eq, cyc = run([100, 80, 75, 95], htp_r=0.5)
    assert cyc[0, 1] == 3
    assert abs(cyc[0, 0] - 5.0) < 1e-9


def test_reversal_close_from_four_positions():
    # hedge_tp を大きくして救済を残し、4 本目 (70) を持った後 65 から 3 戻った 68 で全決済
    eq, cyc = run([100, 65, 75], htp_r=10.0)
    assert cyc[0, 1] == 4
    lots = [1.0, 1.5, 2.5, 3.5]  # 2.25→2.5, 3.375→3.5
    pnl = (68 - 100) * lots[0] + (68 - 90) * lots[1] + (80 - 68) * lots[2] + (68 - 70) * lots[3]
    assert abs(cyc[0, 0] - pnl) < 1e-9


def test_sell_side_mirror_and_spread():
    # 売り 100 (ビッド), スプレッド 1。アスク 94 で +5 → ビッド 93
    eq, cyc = run([100, 90], sig0=-1, spread=1.0)
    assert abs(cyc[0, 0] - 5.0) < 1e-9


def test_max_loss_stop():
    eq, cyc = run([100, 50], max_pos=2, max_loss_money=30.0)
    assert abs(cyc[0, 0] + 30.0) < 1e-9
