"""エンジンの検算。

- CCI が定義どおりか
- 上位足は確定足しか使っていないか
- EA (CCIPullbackEA.mq5) と同じ手順 (新しい足ごとに、確定足だけを見て判定・保有中は
  エントリー足から状態を再計算) で1本ずつ再生した結果が、エンジンの取引と一致するか

実行: python3 -m pytest tests -q  (fx/ea/cci_pullback で)
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cci_bt import (SYMBOLS, TF_MIN, TP_MODES, Params, build_frame, cci, ema,  # noqa: E402
                    run)
from data import DATA, bars  # noqa: E402

pytestmark = pytest.mark.skipif(not (DATA / "XAUUSD_1m.csv.gz").exists(),
                                reason="data/ が無い (fetch_data.py を実行)")


def test_cci_formula():
    rng = np.random.default_rng(1)
    c = 100 + rng.standard_normal(300).cumsum()
    h = c + rng.random(300)
    l = c - rng.random(300)
    tp = pd.Series((h + l + c) / 3)
    m = tp.rolling(14).mean()
    md = tp.rolling(14).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    ref = (tp - m) / (0.015 * md)
    np.testing.assert_allclose(cci(h, l, c, 14)[13:], ref[13:].to_numpy(), rtol=1e-9)


def test_htf_only_closed_bars():
    p = Params(ltf="M15", htf="H1")
    d = build_frame(bars("XAUUSD", "M15"), bars("XAUUSD", "H1"), p)
    d = d.dropna(subset=["avail"])
    assert (d["avail"] <= d["close_time"]).all()
    # 使っている上位足は、下位足の確定時刻までに確定した最新の足
    H = bars("XAUUSD", "H1")
    avail = (H.time + pd.Timedelta(minutes=TF_MIN["H1"])).to_numpy()
    latest = avail[np.searchsorted(avail, d["close_time"].to_numpy(), side="right") - 1]
    assert (d["avail"].to_numpy() == latest).all()


# ---------------------------------------------------------------- EA と同じ手順の再生
def _swing_env(hh, hl, hc, k, env_cci_val, env_cci_min):
    sh = [None, None]
    sl = [None, None]
    n = len(hh)
    for i in range(k, n - k):
        isH = all(hh[i] > hh[j] for j in range(i - k, i)) and all(hh[i] >= hh[j] for j in range(i + 1, i + k + 1))
        isL = all(hl[i] < hl[j] for j in range(i - k, i)) and all(hl[i] <= hl[j] for j in range(i + 1, i + k + 1))
        if isH:
            sh = [sh[1], hh[i]]
        if isL:
            sl = [sl[1], hl[i]]
    if sh[0] is None or sl[0] is None:
        return False
    ok = sh[1] > sh[0] and sl[1] > sl[0] and hc[-1] > sl[1]
    return ok and env_cci_val >= env_cci_min


def _replay(d: pd.DataFrame, H: pd.DataFrame, p: Params, spread: float, n_bars: int):
    """EA の OnTick を新しい足ごとに再生 (緊急損切は足の途中で判定)。"""
    o, h, l, c = (d[x].to_numpy() for x in ("open", "high", "low", "close"))
    cc, ax, at = d["cci"].to_numpy(), d["adx"].to_numpy(), d["atr"].to_numpy()
    hcci = cci(H.high.to_numpy(), H.low.to_numpy(), H.close.to_numpy(), 14)
    havail = (H.time + pd.Timedelta(minutes=TF_MIN[p.htf])).to_numpy()
    ctime = d["close_time"].to_numpy()
    trades = []
    pos = None  # (entry_bar, entry_price, sl)
    tpm = TP_MODES[p.tp_mode]
    for b in range(30, n_bars):          # b = 新しく始まった足, b-1 = 確定足
        if pos is not None:
            e, ent, sl = pos
            # 足 b-1 の途中で緊急損切/指値に掛かったか (EA ではブローカー側で約定)
            tpp = ent + 2 * (ent - sl) if tpm == 7 else -1
            if sl > 0 and l[b - 1] <= sl:
                trades.append((e, b - 1))
                pos = None
            elif tpp > 0 and h[b - 1] >= tpp:
                trades.append((e, b - 1))
                pos = None
            else:
                reached, fast, peak, hh = False, False, -1e18, h[e]
                for j in range(e, b):
                    x = cc[j]
                    new_high = h[j] > hh and j > e
                    if x >= p.tp_level and not reached:
                        reached = True
                        fast = (x - cc[j - 3]) >= p.angle_th
                    out = False
                    if x < -100 and c[j] < o[j]:
                        out = True
                    elif reached and j > e:
                        out = {1: lambda: x < p.tp_level, 2: lambda: x < cc[j - 1], 3: lambda: ax[j] < ax[j - 1],
                               4: lambda: h[j - p.range_n + 1:j + 1].max() - l[j - p.range_n + 1:j + 1].min()
                               < p.range_k * at[j],
                               5: lambda: (new_high and x < peak) or x < 0, 6: lambda: x < 0,
                               8: lambda: (x < cc[j - 1]) if fast else (x < 0)}.get(tpm, lambda: False)()
                    if reached and x > peak:
                        peak = x
                    hh = max(hh, h[j])
                    if out:
                        break
                if out:
                    trades.append((e, b))
                    pos = None
                continue  # 決済した足では新規エントリーしない
        if pos is not None:
            continue
        i = b - 1
        if not (cc[i - 1] <= p.trig_level < cc[i]):
            continue
        # 上位足: この時点までに確定した足 (最大500本)
        m = np.searchsorted(havail, ctime[i], side="right")
        lo = max(0, m - 500)
        if not _swing_env(H.high.to_numpy()[lo:m], H.low.to_numpy()[lo:m], H.close.to_numpy()[lo:m], 2,
                          hcci[m - 1], p.env_cci):
            continue
        low_min = l[i - p.sl_lookback + 1:i + 1].min()
        sl = low_min - 0.2 * at[i]
        if o[b] <= sl:
            continue
        pos = (b, o[b] + spread, sl)
    return trades


@pytest.mark.parametrize("tp_mode", ["diverge", "range", "cci_peak", "angle", "fixed_2R", "adx_fall"])
def test_replay_matches_engine(tp_mode):
    p = Params(ltf="M15", htf="H1", tp_mode=tp_mode)
    L, H = bars("XAUUSD", "M15"), bars("XAUUSD", "H1")
    d = build_frame(L, H, p)
    n = 6000
    tr = run(d.iloc[:n].reset_index(drop=True), p, SYMBOLS["XAUUSD"])
    tr = tr[tr.reason != "期間終了"]
    t = d["time"].to_numpy()
    rep = _replay(d, H, p, SYMBOLS["XAUUSD"].spread, n)
    got = [(t[e], t[x]) for e, x in rep]
    exp = list(zip(tr.entry_time.to_numpy(), tr.exit_time.to_numpy()))
    assert len(exp) >= 10
    assert got[:len(exp)] == exp


def test_ema_matches_recursive():
    x = np.arange(10, dtype=float)
    e = ema(x, 3)
    assert e[0] == 0 and abs(e[1] - 0.5) < 1e-12
