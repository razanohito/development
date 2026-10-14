"""エンジンの検算: 価格を $0.01 刻みのティックに展開し、EA と同じ OnTick ロジックを
素朴に実装したリファレンスと結果を突き合わせる。"""
import math, os, sys
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backtest"))
from nanpin_bt import Backtester, Market, Params, load_m1, CONTRACT

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "XAUUSD_1m_2026-03-26_2026-09-25.csv.gz")


def lot_n(p, n):
    return max(0.01, math.floor(p.init_lot * p.multiplier ** n / 0.01 + 0.5 + 1e-9) * 0.01)


def reference(bt, p, i0, i1):
    m = bt.m
    sp = m.spread
    bal = bt.balance_usd
    tp = p.tp_move * p.init_lot * CONTRACT
    buys, sells = [], []  # (lot, price)
    peak, maxdd = bal, 0.0
    cool = -1
    o, h, l, c, filt, mins = bt.o, bt.h, bt.l, bt.c, bt.filt[p.filter_id], bt.minutes
    use_b = p.direction in (0, 2); use_s = p.direction in (1, 2)
    prev = o[i0]
    for i in range(i0, i1):
        fs = filt[i]
        ab = use_b and fs in (1, 2); as_ = use_s and fs in (-1, 2)
        pts = [o[i]] + ([l[i], h[i]] if c[i] >= o[i] else [h[i], l[i]]) + [c[i]]
        ticks = [o[i]]
        for a, b in zip(pts[:-1], pts[1:]):
            n = int(round(abs(b - a) / 0.01))
            if n:
                ticks += list(np.linspace(a, b, n + 1)[1:])
        for bid in ticks:
            ask = bid + sp
            def eq():
                return (bal + sum(lt * (bid - pr) for lt, pr in buys) * CONTRACT
                        + sum(lt * (pr - ask) for lt, pr in sells) * CONTRACT)
            if mins[i] >= cool:
                if ab and not buys: buys.append((lot_n(p, 0), ask))
                if as_ and not sells: sells.append((lot_n(p, 0), bid))
            # TP
            if buys and sum(lt * (bid - pr) for lt, pr in buys) * CONTRACT >= tp - 1e-9:
                bal += sum(lt * (bid - pr) for lt, pr in buys) * CONTRACT; buys = []
                if mins[i] >= cool and ab: buys.append((lot_n(p, 0), ask))
            if sells and sum(lt * (pr - ask) for lt, pr in sells) * CONTRACT >= tp - 1e-9:
                bal += sum(lt * (pr - ask) for lt, pr in sells) * CONTRACT; sells = []
                if mins[i] >= cool and as_: sells.append((lot_n(p, 0), bid))
            # 口座損切り
            if p.stop_pct > 0 and (buys or sells) and eq() <= bal * (1 - p.stop_pct) + 1e-9:
                bal = eq(); buys = []; sells = []; cool = mins[i] + p.cooldown_min
            # ナンピン
            if buys and len(buys) < p.max_pos and bid <= buys[-1][1] - sp - p.grid_step + 1e-9:
                buys.append((lot_n(p, len(buys)), ask))
            if sells and len(sells) < p.max_pos and bid >= sells[-1][1] + p.grid_step - 1e-9:
                sells.append((lot_n(p, len(sells)), bid))
            e = eq(); peak = max(peak, e); maxdd = max(maxdd, (peak - e) / peak)
        prev = c[i]
    bid = c[i1 - 1]; ask = bid + sp
    final = (bal + sum(lt * (bid - pr) for lt, pr in buys) * CONTRACT
             + sum(lt * (pr - ask) for lt, pr in sells) * CONTRACT)
    return final, maxdd


def test_matches_reference():
    df = load_m1(DATA)
    # スワップ・ロスカットは参照実装で扱わないので無効化
    bt = Backtester(df, Market(swap_long=0, swap_short=0, leverage=1e9))
    i0, i1 = bt.index_range("2026-06-01", "2026-06-06")
    cases = [
        Params(direction=0, init_lot=0.05, grid_step=10, max_pos=10, tp_move=10),
        Params(direction=1, init_lot=0.03, grid_step=5, max_pos=8, tp_move=5),
        Params(direction=2, init_lot=0.02, grid_step=15, max_pos=15, tp_move=20, stop_pct=0.01),
        Params(direction=0, init_lot=0.10, grid_step=20, max_pos=5, tp_move=10, filter_id=1),
    ]
    for p in cases:
        r = bt.run(p, "2026-06-01", "2026-06-06")
        ref_final, ref_dd = reference(bt, p, i0, i1)
        eng_final = bt.balance_usd * (1 + r["return_pct"] / 100)
        print(p.direction, p.init_lot, "engine", round(eng_final, 2), round(r["maxdd_pct"], 3),
              "ref", round(ref_final, 2), round(ref_dd * 100, 3))
        assert abs(eng_final - ref_final) < max(5.0, 0.001 * bt.balance_usd), (eng_final, ref_final)
        assert abs(r["maxdd_pct"] - ref_dd * 100) < 0.3




def test_trading_resumes_after_account_stop():
    df = load_m1(DATA)
    bt = Backtester(df, Market())
    assert np.diff(bt.minutes).min() >= 1  # 分単位の時刻になっていること
    p = Params(direction=1, init_lot=0.1, grid_step=50, max_pos=5, tp_move=5, stop_pct=0.05,
               cooldown_min=1440)
    r = bt.run(p, "2026-07-06", "2026-09-26", curve=True)
    assert r["n_stop"] >= 1
    cv = r["curve"]
    changes = cv.diff().abs() > 0
    # 損切り後も取引が再開され、期間後半まで損益が動いている
    assert changes[changes].index[-1] > np.datetime64("2026-09-01")


if __name__ == "__main__":
    test_matches_reference()
    test_trading_resumes_after_account_stop()
    print("OK")
