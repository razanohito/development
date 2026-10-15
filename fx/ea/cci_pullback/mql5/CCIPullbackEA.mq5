//+------------------------------------------------------------------+
//|                                              CCIPullbackEA.mq5   |
//|  CCI 押し目買い (風林火山) EA                                       |
//|                                                                  |
//|  ① 環境認識 (上位足): スイング高値・安値の切り上がり + 上位足CCI    |
//|  ② トリガー (下位足): CCI が -100 以下から -100 を上抜け            |
//|  ③ 損切: CCI 再び -100 割れ + 陰線確定 / 緊急損切 (押し安値)        |
//|     利確: CCI +100 到達後、勢いが無くなった時 (9通りから選択)        |
//|                                                                  |
//|  ロジックは fx/ea/cci_pullback/cci_bt.py と同じ。                    |
//|  シグナルは確定足で判定し、次の足の始値 (新しい足の最初のティック)で |
//|  売買する。買いのみ・同時に1ポジション。                            |
//+------------------------------------------------------------------+
#property copyright "razanohito/development"
#property version   "1.00"

#include <Trade/Trade.mqh>

//--- 列挙型
enum ENUM_ENV_MODE
  {
   ENV_STRUCTURE = 0, // スイング構造 (高値更新・押し安値維持)
   ENV_EMA       = 1, // 上位足 EMA50 の上 & EMA 上向き
   ENV_NONE      = 2  // 環境認識なし
  };

enum ENUM_TP_MODE
  {
   TP_NONE     = 0, // 利確なし (損切ルールのみ)
   TP_CCI_BACK = 1, // CCI が +100 を割り込む
   TP_CCI_PEAK = 2, // CCI がピークアウト (前の足より低下)
   TP_ADX_FALL = 3, // ADX が低下
   TP_RANGE    = 4, // 値幅が縮小 (直近N本の高安幅 < k*ATR)
   TP_DIVERGE  = 5, // ダイバージェンス or CCI<0
   TP_CCI_ZERO = 6, // CCI が 0 を割り込む
   TP_FIXED_2R = 7, // 固定 2R 指値 (緊急損切が必要)
   TP_ANGLE    = 8  // CCI 角度で切替 (急→ピークアウト / 緩→0割れ)
  };

//--- 入力パラメータ (既定値 = 検証での推奨設定: XAUUSD M15 / H1)
input group "=== 基本 ==="
input double          InpLot            = 0.01;          // ロット
input ENUM_TIMEFRAMES InpEntryTF        = PERIOD_M15;    // エントリー足 (下位足)
input ENUM_TIMEFRAMES InpTrendTF        = PERIOD_H1;     // 環境認識足 (上位足)
input int             InpCCIPeriod      = 14;            // CCI 期間
input ulong           InpMagic          = 20261015;      // マジックナンバー
input int             InpMaxSpreadPoints= 0;             // 許容スプレッド (ポイント, 0=制限なし)

input group "=== ① 環境認識 (上位足) ==="
input ENUM_ENV_MODE   InpEnvMode        = ENV_STRUCTURE; // 上昇トレンドの判定方法
input double          InpEnvCCIMin      = 50.0;          // 上位足 CCI の下限 (-1000 で無効)
input int             InpSwingK         = 2;             // スイング判定の左右本数
input int             InpTrendBars      = 500;           // スイング判定に使う上位足の本数

input group "=== ② トリガー (下位足) ==="
input double          InpTrigLevel      = -100.0;        // CCI 上抜けレベル
input double          InpMaxDepth       = 0.0;           // CCI 深さ上限 (例 200 → -200 より深い押しは見送り, 0=無効)
input int             InpDepthLookback  = 20;            // 深さ判定の本数
input bool            InpUseCandle      = false;         // 陽線包み足 or 下ヒゲ反発を要求
input bool            InpUseEMATouch    = false;         // 押しが 20EMA 付近まで来たことを要求
input bool            InpUseFib         = false;         // 押しが上位足上昇波の 38.2〜78.6% であることを要求

input group "=== ③ 決済 ==="
input bool            InpStopRule       = true;          // 損切: CCI -100 割れ + 陰線確定で撤退
input bool            InpUseHardSL      = true;          // 緊急損切 (押し安値 - 0.2ATR に逆指値)
input int             InpSLLookback     = 10;            // 押し安値を探す本数
input double          InpSLAtrBuffer    = 0.2;           // 押し安値からの余裕 (ATR 倍)
input ENUM_TP_MODE    InpTPMode         = TP_DIVERGE;    // 利確方法
input double          InpTPLevel        = 100.0;         // 利確判定を始める CCI レベル
input int             InpRangeN         = 6;             // [値幅縮小] 判定本数
input double          InpRangeK         = 2.0;           // [値幅縮小] ATR 倍率
input double          InpAngleTh        = 150.0;         // [角度] 3本で CCI がこれ以上上がったら「急」
input int             InpMaxBars        = 0;             // 最大保有本数 (0=無制限)

//--- グローバル
CTrade   trade;
int      hCCI = INVALID_HANDLE, hATR = INVALID_HANDLE, hADX = INVALID_HANDLE, hEMA = INVALID_HANDLE;
int      hTCCI = INVALID_HANDLE, hTEMA = INVALID_HANDLE;
datetime g_lastBar = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpEntryTF == PERIOD_CURRENT || InpTrendTF == PERIOD_CURRENT)
     {
      Print("エントリー足・環境認識足は明示的に指定してください");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(PeriodSeconds(InpTrendTF) <= PeriodSeconds(InpEntryTF))
     {
      Print("環境認識足はエントリー足より長い時間足にしてください");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpTPMode == TP_FIXED_2R && !InpUseHardSL)
     {
      Print("固定 2R 利確には緊急損切 (InpUseHardSL) が必要です");
      return INIT_PARAMETERS_INCORRECT;
     }
   hCCI  = iCCI(_Symbol, InpEntryTF, InpCCIPeriod, PRICE_TYPICAL);
   hATR  = iATR(_Symbol, InpEntryTF, 14);
   hADX  = iADX(_Symbol, InpEntryTF, 14);
   hEMA  = iMA(_Symbol, InpEntryTF, 20, 0, MODE_EMA, PRICE_CLOSE);
   hTCCI = iCCI(_Symbol, InpTrendTF, 14, PRICE_TYPICAL);
   hTEMA = iMA(_Symbol, InpTrendTF, 50, 0, MODE_EMA, PRICE_CLOSE);
   if(hCCI == INVALID_HANDLE || hATR == INVALID_HANDLE || hADX == INVALID_HANDLE ||
      hEMA == INVALID_HANDLE || hTCCI == INVALID_HANDLE || hTEMA == INVALID_HANDLE)
     {
      Print("インジケーターの作成に失敗しました");
      return INIT_FAILED;
     }
   trade.SetExpertMagicNumber(InpMagic);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetDeviationInPoints(50);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   IndicatorRelease(hCCI);
   IndicatorRelease(hATR);
   IndicatorRelease(hADX);
   IndicatorRelease(hEMA);
   IndicatorRelease(hTCCI);
   IndicatorRelease(hTEMA);
  }

//+------------------------------------------------------------------+
//| この EA のポジションを探す (見つかれば選択状態にして true)          |
//+------------------------------------------------------------------+
bool SelectMyPosition(ulong &ticket)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == InpMagic)
        {
         ticket = t;
         return true;
        }
     }
   return false;
  }

//+------------------------------------------------------------------+
//| バッファを時系列 (index 0 = 最新の確定足) でコピー                   |
//+------------------------------------------------------------------+
bool CopySeries(int handle, int buffer, int count, double &arr[])
  {
   ArraySetAsSeries(arr, true);
   return CopyBuffer(handle, buffer, 1, count, arr) == count;
  }

//+------------------------------------------------------------------+
//| ① 上位足の環境認識                                                 |
//|  ok: 上昇トレンドか / lastSL, lastSH: 直近の押し安値・高値            |
//+------------------------------------------------------------------+
bool TrendEnvironment(bool &ok, double &lastSL, double &lastSH)
  {
   ok = false;
   lastSL = EMPTY_VALUE;
   lastSH = EMPTY_VALUE;
   int n = InpTrendBars;
   double h[], l[], c[];
   // 古い順 (index 0 = 最も古い確定足, n-1 = 直近の確定足)
   ArraySetAsSeries(h, false);
   ArraySetAsSeries(l, false);
   ArraySetAsSeries(c, false);
   if(CopyHigh(_Symbol, InpTrendTF, 1, n, h) != n ||
      CopyLow(_Symbol, InpTrendTF, 1, n, l) != n ||
      CopyClose(_Symbol, InpTrendTF, 1, n, c) != n)
      return false;

   // スイング: high[i] > max(high[i-k..i-1]) かつ high[i] >= max(high[i+1..i+k])
   double sh0 = EMPTY_VALUE, sh1 = EMPTY_VALUE, sl0 = EMPTY_VALUE, sl1 = EMPTY_VALUE;
   int k = InpSwingK;
   for(int i = k; i <= n - 1 - k; i++)
     {
      bool isH = true, isL = true;
      for(int j = i - k; j < i; j++)
        {
         if(h[i] <= h[j]) isH = false;
         if(l[i] >= l[j]) isL = false;
        }
      for(int j = i + 1; j <= i + k; j++)
        {
         if(h[i] < h[j]) isH = false;
         if(l[i] > l[j]) isL = false;
        }
      if(isH) { sh0 = sh1; sh1 = h[i]; }
      if(isL) { sl0 = sl1; sl1 = l[i]; }
     }
   lastSL = sl1;
   lastSH = sh1;
   double cLast = c[n - 1];

   if(InpEnvMode == ENV_STRUCTURE)
     {
      if(sh0 == EMPTY_VALUE || sl0 == EMPTY_VALUE)
         return true;
      ok = (sh1 > sh0 && sl1 > sl0 && cLast > sl1);
     }
   else if(InpEnvMode == ENV_EMA)
     {
      double e[];
      if(!CopySeries(hTEMA, 0, 4, e))
         return false;
      ok = (cLast > e[0] && e[0] > e[3]);
     }
   else
      ok = true;

   if(ok && InpEnvCCIMin > -999.0)
     {
      double tc[];
      if(!CopySeries(hTCCI, 0, 1, tc))
         return false;
      ok = (tc[0] >= InpEnvCCIMin);
     }
   return true;
  }

//+------------------------------------------------------------------+
//| ② 買いシグナル (確定足 = shift 1)                                   |
//+------------------------------------------------------------------+
bool BuySignal(double &slPrice)
  {
   int need = MathMax(MathMax(InpDepthLookback, InpSLLookback), 3) + 1;
   double cci[], atr[], ema[], o[], h[], l[], c[];
   if(!CopySeries(hCCI, 0, need, cci) || !CopySeries(hATR, 0, 1, atr) || !CopySeries(hEMA, 0, 1, ema))
      return false;
   ArraySetAsSeries(o, true);
   ArraySetAsSeries(h, true);
   ArraySetAsSeries(l, true);
   ArraySetAsSeries(c, true);
   if(CopyOpen(_Symbol, InpEntryTF, 1, need, o) != need || CopyHigh(_Symbol, InpEntryTF, 1, need, h) != need ||
      CopyLow(_Symbol, InpEntryTF, 1, need, l) != need || CopyClose(_Symbol, InpEntryTF, 1, need, c) != need)
      return false;
   // ここでは index 0 = 確定したばかりの足 (shift 1)

   if(!(cci[1] <= InpTrigLevel && cci[0] > InpTrigLevel))
      return false;

   bool envOk;
   double lastSL, lastSH;
   if(!TrendEnvironment(envOk, lastSL, lastSH) || !envOk)
      return false;

   if(InpMaxDepth > 0)
     {
      double mn = cci[0];
      for(int i = 1; i < InpDepthLookback; i++)
         mn = MathMin(mn, cci[i]);
      if(mn < -InpMaxDepth)
         return false;
     }

   if(InpUseCandle)
     {
      bool pat = false;
      for(int s = 0; s <= 1; s++)
        {
         bool engulf = (c[s] > o[s] && c[s + 1] < o[s + 1] && c[s] >= o[s + 1] && o[s] <= c[s + 1]);
         double rng = h[s] - l[s];
         bool wick = (MathMin(o[s], c[s]) - l[s]) >= 0.5 * rng;
         if(engulf || wick)
            pat = true;
        }
      if(!pat)
         return false;
     }

   double lowMin = l[0];
   for(int i = 1; i < InpSLLookback; i++)
      lowMin = MathMin(lowMin, l[i]);

   if(InpUseEMATouch && !(lowMin <= ema[0] + 0.5 * atr[0]))
      return false;

   if(InpUseFib)
     {
      if(lastSH == EMPTY_VALUE || lastSL == EMPTY_VALUE)
         return false;
      double wave = lastSH - lastSL;
      if(wave <= 0)
         return false;
      double ret = (lastSH - lowMin) / wave;
      if(ret < 0.382 || ret > 0.786)
         return false;
     }

   slPrice = lowMin - InpSLAtrBuffer * atr[0];
   return true;
  }

//+------------------------------------------------------------------+
//| ③ 保有中の決済判定 (確定足 shift 1 で判定, 状態はエントリー足から再計算) |
//|  戻り値: true = 今すぐ決済                                         |
//+------------------------------------------------------------------+
bool ShouldExit(datetime openTime, string &why)
  {
   int e = iBarShift(_Symbol, InpEntryTF, openTime, false); // エントリーした足
   if(e < 1)
      return false;                                          // まだエントリー足が確定していない
   int cnt = e + 4;                                          // 角度判定に3本前まで必要
   double cci[], adx[], atr[], o[], h[], l[], c[];
   ArraySetAsSeries(cci, true);
   ArraySetAsSeries(adx, true);
   ArraySetAsSeries(atr, true);
   ArraySetAsSeries(o, true);
   ArraySetAsSeries(h, true);
   ArraySetAsSeries(l, true);
   ArraySetAsSeries(c, true);
   // ここでは shift 0 から取得し、配列 index = シフト
   if(CopyBuffer(hCCI, 0, 0, cnt, cci) != cnt || CopyBuffer(hADX, 0, 0, cnt, adx) != cnt ||
      CopyBuffer(hATR, 0, 0, cnt, atr) != cnt ||
      CopyOpen(_Symbol, InpEntryTF, 0, cnt, o) != cnt || CopyHigh(_Symbol, InpEntryTF, 0, cnt, h) != cnt ||
      CopyLow(_Symbol, InpEntryTF, 0, cnt, l) != cnt || CopyClose(_Symbol, InpEntryTF, 0, cnt, c) != cnt)
      return false;
   if(InpTPMode == TP_RANGE && e + InpRangeN > cnt)
     {
      int cnt2 = e + InpRangeN + 1;
      if(CopyHigh(_Symbol, InpEntryTF, 0, cnt2, h) != cnt2 || CopyLow(_Symbol, InpEntryTF, 0, cnt2, l) != cnt2)
         return false;
     }

   bool   reached = false, fast = false;
   double peak = -1e18;
   double hh = h[e];
   for(int j = e; j >= 1; j--)        // 古い足 → 新しい足
     {
      double x = cci[j];
      bool newHigh = (h[j] > hh && j < e);
      if(x >= InpTPLevel && !reached)
        {
         reached = true;
         fast = (x - cci[j + 3]) >= InpAngleTh;
        }
      bool out = false;
      string w = "";
      if(InpStopRule && x < -100.0 && c[j] < o[j])
        {
         out = true;
         w = "損切ルール (CCI-100割れ+陰線)";
        }
      else if(reached && j < e)
        {
         switch(InpTPMode)
           {
            case TP_CCI_BACK: out = (x < InpTPLevel); break;
            case TP_CCI_PEAK: out = (x < cci[j + 1]); break;
            case TP_ADX_FALL: out = (adx[j] < adx[j + 1]); break;
            case TP_RANGE:
              {
               double hi = h[j], lo = l[j];
               for(int r = j + 1; r < j + InpRangeN; r++)
                 {
                  hi = MathMax(hi, h[r]);
                  lo = MathMin(lo, l[r]);
                 }
               out = (hi - lo) < InpRangeK * atr[j];
               break;
              }
            case TP_DIVERGE: out = ((newHigh && x < peak) || x < 0.0); break;
            case TP_CCI_ZERO: out = (x < 0.0); break;
            case TP_ANGLE: out = fast ? (x < cci[j + 1]) : (x < 0.0); break;
            default: out = false;
           }
         if(out)
            w = "利確 (勢い喪失)";
        }
      if(!out && InpMaxBars > 0 && e - j + 1 >= InpMaxBars)
        {
         out = true;
         w = "最大保有本数";
        }
      if(reached && x > peak)
         peak = x;
      if(h[j] > hh)
         hh = h[j];
      if(out)
        {
         // 過去の足で決済条件が出ていた場合 (EA 停止中など) も、今決済する
         why = w;
         return true;
        }
     }
   return false;
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   datetime bar = iTime(_Symbol, InpEntryTF, 0);
   if(bar == 0 || bar == g_lastBar)
      return;
   g_lastBar = bar;

   ulong ticket;
   if(SelectMyPosition(ticket))
     {
      datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
      string why;
      if(ShouldExit(openTime, why))
        {
         if(trade.PositionClose(ticket))
            PrintFormat("決済: %s", why);
         else
           {
            PrintFormat("決済に失敗: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
            g_lastBar = 0; // 次のティックで再試行
           }
        }
      return; // 決済した足では新規エントリーしない (検証と同じ)
     }

   double sl;
   if(!BuySignal(sl))
      return;

   MqlTick tk;
   if(!SymbolInfoTick(_Symbol, tk))
      return;
   if(InpMaxSpreadPoints > 0 && (tk.ask - tk.bid) / _Point > InpMaxSpreadPoints)
     {
      Print("スプレッドが広いため見送り");
      return;
     }
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double slOrder = 0.0, tpOrder = 0.0;
   if(InpUseHardSL)
     {
      if(tk.bid <= sl)
         return; // すでに損切価格より下 (検証と同じく見送り)
      double minDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
      slOrder = MathMin(sl, tk.bid - minDist);
      slOrder = NormalizeDouble(slOrder, digits);
      if(InpTPMode == TP_FIXED_2R)
         tpOrder = NormalizeDouble(tk.ask + 2.0 * (tk.ask - sl), digits);
     }

   double lot = InpLot;
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step > 0)
      lot = MathFloor(lot / step + 1e-9) * step;
   lot = MathMax(vmin, MathMin(vmax, lot));

   if(trade.Buy(lot, _Symbol, tk.ask, slOrder, tpOrder, "CCI pullback"))
      Print("買い ", DoubleToString(lot, 2), " lot @ ", DoubleToString(tk.ask, digits), "  SL=", DoubleToString(slOrder, digits));
   else
     {
      PrintFormat("買い注文に失敗: %d %s", trade.ResultRetcode(), trade.ResultRetcodeDescription());
     }
  }
//+------------------------------------------------------------------+
