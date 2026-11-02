"""TrendNanpinHedgeEA (mql5/TrendNanpinHedgeEA.mq5) と同じルールのバックテスター。

ルール (EA と共通。間隔は固定 pips か、日足 ATR × 係数)
  1. 初回エントリー: シグナル (AI の代わりのルール、または外部シグナル) の方向 D に Lot0 で 1 本目。
  2. 順張りナンピン: 直前のエントリー位置から StepPips 逆行するごとに次の 1 本。
     ロットは Lot0 × Mult^(n-1) をロット刻みに丸める (0.01→0.02→0.02→0.03→0.05)。
  3. 逆張り救済: HedgeFrom 本目 (既定 3 本目) は D と逆方向に建てる。
     逆方向ポジションは個別に HedgeTPPips の利益で利確する。
     HedgeFrom より後 (4・5 本目) の向きは AfterHedge で選ぶ (0=元の方向にナンピン, 1=逆方向を続ける)。
  4. 決済: サイクル損益 (確定済みの救済利益 + 含み損益) が BasketTPPips×Lot0 相当に達したら全決済
     (基本 1〜2 本で決済)。
     RevFrom 本 (既定 4 本) 以上持ったら、逆行の最安値 (買いの場合) から RevPips 戻った時点で全決済。
  5. 安全装置 (任意): サイクル損失が MaxLossMoney に達したら全決済。0 で無効。

価格はビッド。買いはアスク (=ビッド+スプレッド) で建てビッドで決済、売りはその逆。
バーの中は 始値→(始値に近い方の高値/安値)→もう一方→終値 の順に動いたとみなし、
到達した価格ぴったりで約定させる (ギャップ時は始値で約定)。
"""
from __future__ import annotations

import numpy as np
from numba import njit

MAXP = 8


@njit(cache=True)
def _round_lot(x, lot_step):
    n = np.floor(x / lot_step + 0.5)
    if n < 1:
        n = 1
    return n * lot_step


@njit(cache=True)
def simulate(o, h, l, c, sig, pip, spread, cs,
             lot0, mult, max_pos, step_arr, hedge_from, after_hedge, htp_r,
             tp_r, rev_from, rev_r, max_loss_money, lot_step):
    """step_arr: バーごとのナンピン間隔 (pips)。サイクル開始時の値をそのサイクル中使う。
    htp_r / tp_r / rev_r: 逆方向利確・バスケット利確・反転幅を間隔に対する比率で指定。
    戻り値: equity (各バー終値時点の累計損益, 口座通貨), cycles (n,4: 損益, 最大本数, 開始バー, 終了バー)"""
    n = len(o)
    step = 0.0
    htp = 0.0
    rev = 0.0
    tp_money = 0.0
    act = np.zeros(MAXP, np.bool_)
    pdir = np.zeros(MAXP, np.int64)
    plot = np.zeros(MAXP)
    pent = np.zeros(MAXP)
    equity = np.zeros(n)
    cyc = np.zeros((n, 4))
    ncyc = 0
    closed = 0.0
    in_cycle = False
    D = 0
    nlev = 0
    ref = 0.0
    realized = 0.0
    extreme = 0.0
    start_bar = 0
    pts = np.zeros(4)

    for i in range(n):
        # ---- 新規サイクル (バー始値で判定) ----
        if not in_cycle and sig[i] != 0:
            in_cycle = True
            D = sig[i]
            nlev = 1
            act[:] = False
            act[0] = True
            pdir[0] = D
            plot[0] = lot0
            pent[0] = o[i] + spread if D > 0 else o[i]
            ref = o[i]
            realized = 0.0
            extreme = o[i]
            start_bar = i
            step = step_arr[i] * pip
            htp = step * htp_r
            rev = step * rev_r
            tp_money = step * tp_r * lot0 * cs
        # ---- バー内の経路 ----
        if o[i] - l[i] < h[i] - o[i]:
            pts[0] = o[i]; pts[1] = l[i]; pts[2] = h[i]; pts[3] = c[i]
        else:
            pts[0] = o[i]; pts[1] = h[i]; pts[2] = l[i]; pts[3] = c[i]
        cur = pts[0]
        seg = 0
        gap = True  # 最初は始値そのもので判定 (ギャップ約定)
        while in_cycle:
            if gap:
                a = cur
                b = cur
            else:
                if seg >= 3:
                    break
                a = cur
                b = pts[seg + 1]
            s = 0
            if b > a:
                s = 1
            elif b < a:
                s = -1
            # 各イベントの到達価格を求め、a から b 方向で最も近いものを選ぶ
            best_d = 1e18
            ev = -1
            ev_k = -1
            ev_p = 0.0
            # 1) 次のエントリー
            if nlev < max_pos:
                lv = ref - D * step
                if (D > 0 and a <= lv) or (D < 0 and a >= lv):
                    d = 0.0
                    ev, ev_p, best_d = 1, a, d
                elif s == -D:
                    d = abs(lv - a)
                    if d <= abs(b - a) and d < best_d:
                        ev, ev_p, best_d = 1, lv, d
            # 2) 逆方向ポジションの利確
            for k in range(MAXP):
                if act[k] and pdir[k] != D:
                    if pdir[k] > 0:
                        tp = pent[k] + htp
                        hit_now = a >= tp
                    else:
                        tp = pent[k] - spread - htp
                        hit_now = a <= tp
                    if hit_now:
                        if best_d > 0.0 or ev == -1:
                            ev, ev_k, ev_p, best_d = 2, k, a, 0.0
                    elif s == pdir[k]:
                        d = abs(tp - a)
                        if d <= abs(b - a) and d < best_d:
                            ev, ev_k, ev_p, best_d = 2, k, tp, d
            # 3) バスケット利確 / 安全装置: net(p) = C + K p
            K = 0.0
            C = realized
            for k in range(MAXP):
                if act[k]:
                    if pdir[k] > 0:
                        K += plot[k] * cs
                        C -= plot[k] * cs * pent[k]
                    else:
                        K -= plot[k] * cs
                        C += plot[k] * cs * (pent[k] - spread)
            net_a = C + K * a
            for which in range(2):
                if which == 0:
                    target = tp_money
                    ok_now = net_a >= target
                else:
                    if max_loss_money <= 0:
                        continue
                    target = -max_loss_money
                    ok_now = net_a <= target
                if ok_now:
                    if best_d > 0.0 or ev == -1:
                        ev, ev_p, best_d = 3, a, 0.0
                elif K != 0.0 and s != 0:
                    p = (target - C) / K
                    if (p - a) * s >= 0:
                        d = abs(p - a)
                        if d <= abs(b - a) and d < best_d:
                            ev, ev_p, best_d = 3, p, d
            # 4) 反転決済 (RevFrom 本以上)
            if nlev >= rev_from:
                rp = extreme + D * rev
                if (D > 0 and a >= rp) or (D < 0 and a <= rp):
                    if best_d > 0.0 or ev == -1:
                        ev, ev_p, best_d = 4, a, 0.0
                elif s == D:
                    d = abs(rp - a)
                    if d <= abs(b - a) and d < best_d:
                        ev, ev_p, best_d = 4, rp, d
            if ev == -1:
                # イベントなし: 区間を進めて極値更新
                if not gap:
                    if (b - extreme) * D < 0:
                        extreme = b
                    cur = b
                    seg += 1
                gap = False
                continue
            # 区間の途中までの極値更新
            if (ev_p - extreme) * D < 0:
                extreme = ev_p
            cur = ev_p
            if ev == 1:
                nlev += 1
                dirk = D
                if nlev >= hedge_from:
                    if nlev == hedge_from or after_hedge == 1:
                        dirk = -D
                lot = _round_lot(lot0 * mult ** (nlev - 1), lot_step)
                for k in range(MAXP):
                    if not act[k]:
                        act[k] = True
                        pdir[k] = dirk
                        plot[k] = lot
                        pent[k] = ev_p + spread if dirk > 0 else ev_p
                        break
                ref = ev_p
            elif ev == 2:
                k = ev_k
                if pdir[k] > 0:
                    realized += (ev_p - pent[k]) * plot[k] * cs
                else:
                    realized += (pent[k] - ev_p - spread) * plot[k] * cs
                act[k] = False
            else:
                net = C + K * ev_p
                closed += net
                cyc[ncyc, 0] = net
                cyc[ncyc, 1] = nlev
                cyc[ncyc, 2] = start_bar
                cyc[ncyc, 3] = i
                ncyc += 1
                act[:] = False
                in_cycle = False
        # ---- バー終値の評価 ----
        fl = 0.0
        if in_cycle:
            fl = realized
            for k in range(MAXP):
                if act[k]:
                    if pdir[k] > 0:
                        fl += (c[i] - pent[k]) * plot[k] * cs
                    else:
                        fl += (pent[k] - c[i] - spread) * plot[k] * cs
        equity[i] = closed + fl
    # 終了時に残っていれば最終値で決済
    if in_cycle:
        cyc[ncyc, 0] = equity[n - 1] - closed
        cyc[ncyc, 1] = nlev
        cyc[ncyc, 2] = start_bar
        cyc[ncyc, 3] = n - 1
        ncyc += 1
    return equity, cyc[:ncyc]


def stats(equity: np.ndarray, cyc: np.ndarray, jpy: float = 150.0) -> dict:
    peak = np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    dd = float(np.max(peak - equity)) if len(equity) else 0.0
    pnl = cyc[:, 0] if len(cyc) else np.zeros(0)
    gw = pnl[pnl > 0].sum()
    gl = -pnl[pnl < 0].sum()
    lv = cyc[:, 1] if len(cyc) else np.zeros(0)
    return {
        "cycles": int(len(pnl)),
        "profit_jpy": float(equity[-1]) * jpy,
        "maxdd_jpy": dd * jpy,
        "pf": float(gw / gl) if gl > 0 else float("inf"),
        "win": float((pnl > 0).mean()) if len(pnl) else 0.0,
        "le2": float((lv <= 2).mean()) if len(lv) else 0.0,
        "ge4": float((lv >= 4).mean()) if len(lv) else 0.0,
        "worst_jpy": float(pnl.min()) * jpy if len(pnl) else 0.0,
    }
