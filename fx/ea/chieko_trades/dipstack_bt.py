"""押し目まとめ買い (固定ロット・買い下がり・バスケット利確) のバックテスター。

Chieko さんの取引履歴 (trades.csv) から推定したルールを機械化したもの。
mql5/GoldDipStackEA.mq5 と同じロジック:

* ポジションが無いとき、直近 LookbackMin 分の高値から DipUSD 以上下げたら
  (TrendFilter=1 なら H1 終値が H1 EMA より上のときだけ) 1 段目を買う。
* 1 段 = PositionsPerLevel 本 x Lots。
* 最後に買った段から GridStepUSD 下げるごとに次の段を同じロットで追加 (最大 MaxLevels 段)。
* 平均建値 + TakeProfitUSD で全決済。StopLossUSD > 0 なら平均建値 - StopLossUSD で全決済。
* MaxHoldHours > 0 なら 1 段目からその時間が過ぎた足の終値で全決済 (塩漬け防止)。
* 損切り・時間切れのあと CooldownHours は新規エントリーしない。
* トレンドフィルタ: 0 = なし, 1 = 確定 H1 終値 > H1 EMA50, 2 = 確定 H4 終値 > H4 SMA100
  (金ナンピンEA の探索で最も安定していたフィルタ)。
* 1 分足の中は 陽線: 始値→安値→高値→終値 / 陰線: 始値→高値→安値→終値 の順に動いたとみなす。
* スプレッドは買値に上乗せ、スワップは毎日 21:00 UTC に建玉ロットへ課金 (水曜は 3 日分)。
"""
from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
from numba import njit

DATA = pathlib.Path(__file__).resolve().parent / "data"
CONTRACT = 100.0  # 1 ロット = 100 オンス


def load_m1() -> pd.DataFrame:
    df = pd.read_csv(DATA / "XAUUSD_1m.csv.gz")
    df["time"] = pd.to_datetime(df["datetime"], utc=True).dt.tz_localize(None)
    return df[["time", "open", "high", "low", "close"]].sort_values("time").reset_index(drop=True)


def features(m1: pd.DataFrame, lookback: int, ema_period: int) -> tuple[np.ndarray, np.ndarray]:
    """各 1 分足の始値時点で使える値 (直近 lookback 本の高値, 確定済み H1 の終値 - EMA)。"""
    roll_high = m1["high"].rolling(lookback, min_periods=1).max().shift(1).to_numpy()
    return roll_high, trend_series(m1, "1h", ema_period, "ema")


def trend_series(m1: pd.DataFrame, rule: str, period: int, kind: str) -> np.ndarray:
    """確定済みの上位足終値 - 移動平均 を 1 分足に割り当てる (正 = 上昇トレンド)。"""
    hx = m1.set_index("time")["close"].resample(rule).last().dropna()
    ma = hx.ewm(span=period, adjust=False).mean() if kind == "ema" else hx.rolling(period).mean()
    trend = hx - ma
    # 上位足は足の長さだけ後に確定するので、確定時刻 (開始 + 足の長さ) 以降の 1 分足に割り当てる
    trend.index = trend.index + pd.Timedelta(rule)
    trend_m1 = trend.reindex(m1["time"], method="ffill").to_numpy()
    return np.nan_to_num(trend_m1, nan=-1.0)


def minutes(m1: pd.DataFrame) -> np.ndarray:
    """1 分足の開始時刻 (エポックからの分)。保有時間・クールダウンの判定に使う。"""
    return m1["time"].to_numpy().astype("datetime64[m]").astype(np.int64)


@njit(cache=True)
def run(o, h, l, c, rollover, weekday, roll_high, trend,
        lots, per_level, dip, step, max_levels, tp, sl, use_trend,
        spread, swap_per_lot_day, start, end, tmin, max_hold_min=0, cooldown_min=0):
    """戻り値: (バスケットごとの損益 USD, 最大DD USD, 最大建玉ロット, 足ごとの含み損込み損益 USD,
    バスケットごとの [建て始めの足, 決済の足])"""
    n = len(o)
    basket_pnl = np.zeros(n)
    basket_span = np.zeros((n, 2), dtype=np.int64)
    open_i = 0
    nb = 0
    eq_curve = np.zeros(n)
    levels = 0
    sum_px = 0.0
    last_level = 0.0
    realized = 0.0
    swap_acc = 0.0
    peak = 0.0
    max_dd = 0.0
    max_lots = 0.0
    pts = np.zeros(4)
    resume_at = -1  # この分以降なら新規エントリーしてよい
    for i in range(start, end):
        size = lots * per_level
        # スワップ (21:00 UTC を跨いだ最初の足)
        if levels > 0 and rollover[i]:
            mult = 3.0 if weekday[i] == 2 else 1.0
            swap_acc -= swap_per_lot_day * mult * size * levels
        # 新規エントリー (始値で判定)
        if (levels == 0 and tmin[i] >= resume_at and o[i] <= roll_high[i] - dip
                and (use_trend == 0 or trend[i] > 0.0)):
            levels = 1
            open_i = i
            last_level = o[i] + spread
            sum_px = last_level
        pts[0] = o[i]
        if c[i] >= o[i]:
            pts[1] = l[i]; pts[2] = h[i]
        else:
            pts[1] = h[i]; pts[2] = l[i]
        pts[3] = c[i]
        for k in range(1, 4):
            a = pts[k - 1]
            b = pts[k]
            if levels == 0:
                continue
            if b < a:  # 下落区間: 買い増し → 損切り
                while levels < max_levels and b + spread <= last_level - step:
                    last_level = last_level - step
                    levels += 1
                    sum_px += last_level
                if sl > 0.0:
                    avg = sum_px / levels
                    if b <= avg - sl:
                        pnl = (avg - sl - avg) * CONTRACT * size * levels + swap_acc
                        realized += pnl
                        basket_pnl[nb] = pnl; basket_span[nb, 0] = open_i; basket_span[nb, 1] = i; nb += 1
                        levels = 0; sum_px = 0.0; swap_acc = 0.0
                        resume_at = tmin[i] + cooldown_min
            else:  # 上昇区間: 利確
                avg = sum_px / levels
                if b >= avg + tp:
                    pnl = tp * CONTRACT * size * levels + swap_acc
                    realized += pnl
                    basket_pnl[nb] = pnl; basket_span[nb, 0] = open_i; basket_span[nb, 1] = i; nb += 1
                    levels = 0; sum_px = 0.0; swap_acc = 0.0
        if levels * size > max_lots:
            max_lots = levels * size
        # 足の安値での含み損込み資産で DD を測る
        worst = realized
        if levels > 0:
            avg = sum_px / levels
            worst = realized + (l[i] - avg) * CONTRACT * size * levels + swap_acc
        if realized > peak:
            peak = realized
        if peak - worst > max_dd:
            max_dd = peak - worst
        eq_curve[i] = worst
        # 時間切れ: 1 段目から max_hold_min 分を過ぎたら足の終値で全決済
        if levels > 0 and max_hold_min > 0 and tmin[i] - tmin[open_i] >= max_hold_min:
            avg = sum_px / levels
            pnl = (c[i] - avg) * CONTRACT * size * levels + swap_acc
            realized += pnl
            basket_pnl[nb] = pnl; basket_span[nb, 0] = open_i; basket_span[nb, 1] = i; nb += 1
            levels = 0; sum_px = 0.0; swap_acc = 0.0
            if pnl < 0.0:
                resume_at = tmin[i] + cooldown_min
    # 期間末に残った建玉は終値で決済
    if levels > 0:
        avg = sum_px / levels
        pnl = (c[end - 1] - avg) * CONTRACT * lots * per_level * levels + swap_acc
        realized += pnl
        basket_pnl[nb] = pnl; basket_span[nb, 0] = open_i; basket_span[nb, 1] = end - 1; nb += 1
    return basket_pnl[:nb], max_dd, max_lots, eq_curve, basket_span[:nb]


def prepare(m1: pd.DataFrame):
    # 取引日 = 21:00 UTC 区切り。日付が変わった最初の足でスワップを課金する
    day = (m1["time"] - pd.Timedelta(hours=21)).dt.floor("D")
    rollover = (day != day.shift(1)).to_numpy().copy()
    rollover[0] = False
    weekday = day.dt.weekday.to_numpy().astype(np.int64)  # 跨いだ 21:00 の曜日 (水曜 = 3 日分)
    return (m1["open"].to_numpy(), m1["high"].to_numpy(), m1["low"].to_numpy(), m1["close"].to_numpy(),
            rollover, weekday)
