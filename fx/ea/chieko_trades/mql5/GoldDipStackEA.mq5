//+------------------------------------------------------------------+
//|                                              GoldDipStackEA.mq5  |
//|  押し目まとめ買い + 固定ロット買い下がり + バスケット利確 (買いのみ)  |
//|                                                                  |
//|  Chieko さんの取引履歴 (2026-10-12〜16, GOLD 0.50 ロット x 2〜6 本) |
//|  から推定したルールを機械化したもの。dipstack_bt.py と同じロジック。|
//|                                                                  |
//|  1) ポジションが無いとき、1 分足が確定するたびに                  |
//|     「直近 LookbackMinutes 本の高値 - DipUSD」以下なら              |
//|     PositionsPerLevel 本 x Lots を成行買い (= 1 段目)。            |
//|     UseTrendFilter=true なら確定 H1 終値 > H1 EMA のときだけ。     |
//|  2) 一番安い建値から GridStepUSD 下げるごとに同じロットで 1 段追加 |
//|     (最大 MaxLevels 段。ロットは増やさない)。                      |
//|  3) Bid >= 平均建値 + TakeProfitUSD で全決済。                     |
//|     StopLossUSD > 0 なら Bid <= 平均建値 - StopLossUSD で全決済。  |
//|     AccountStopPercent > 0 なら含み損が残高の X% で全決済。        |
//|                                                                  |
//|  検証結果は results/results.md。損切りなしの設定はすべて           |
//|  0.50 ロット換算で最大DD 500 万円超。実口座で使う前に必ずデモで確認。|
//+------------------------------------------------------------------+
#property copyright "razanohito/development"
#property version   "1.00"

#include <Trade\Trade.mqh>

//--- 入力パラメータ
input group "ロット"
input double Lots               = 0.01;   // 1 本のロット (履歴は 0.50)
input int    PositionsPerLevel  = 3;      // 1 段で買う本数

input group "エントリー"
input int    LookbackMinutes    = 60;     // 押し目判定に使う直近 1 分足の本数
input double DipUSD             = 20.0;   // 直近高値からこのドル数下げたら買う
input bool   UseTrendFilter     = false;  // H1 EMA より上のときだけ買う
input int    TrendEMAPeriod     = 50;     // H1 EMA の期間
input double MaxSpreadUSD       = 0.60;   // これより広いスプレッドでは新規・追加しない

input group "買い下がり"
input double GridStepUSD        = 15.0;   // 一番安い建値からこのドル数下げたら 1 段追加
input int    MaxLevels          = 3;      // 最大段数 (1 = 買い下がりなし)

input group "決済"
input double TakeProfitUSD      = 5.0;    // 平均建値 + このドル数で全決済
input double StopLossUSD        = 30.0;   // 平均建値 - このドル数で全決済 (0 = なし)
input double AccountStopPercent = 0.0;    // 含み損が残高のこの % に達したら全決済 (0 = なし)

input group "その他"
input ulong  MagicNumber        = 20261020;
input int    SlippagePoints     = 50;

//--- グローバル
CTrade   g_trade;
int      g_ema_handle = INVALID_HANDLE;
datetime g_last_m1_bar = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(Lots <= 0.0 || PositionsPerLevel < 1 || MaxLevels < 1 || LookbackMinutes < 1 ||
      DipUSD < 0.0 || GridStepUSD <= 0.0 || TakeProfitUSD <= 0.0 || StopLossUSD < 0.0 ||
      AccountStopPercent < 0.0 || TrendEMAPeriod < 1)
     {
      Print("入力値が不正です");
      return INIT_PARAMETERS_INCORRECT;
     }
   g_trade.SetExpertMagicNumber(MagicNumber);
   g_trade.SetDeviationInPoints(SlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   if(UseTrendFilter)
     {
      g_ema_handle = iMA(_Symbol, PERIOD_H1, TrendEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
      if(g_ema_handle == INVALID_HANDLE)
        {
         Print("H1 EMA を作成できません: ", GetLastError());
         return INIT_FAILED;
        }
     }
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_ema_handle != INVALID_HANDLE)
      IndicatorRelease(g_ema_handle);
  }

//+------------------------------------------------------------------+
//| ロットをシンボルの刻みに合わせる                                  |
//+------------------------------------------------------------------+
double NormalizeLots(double lots)
  {
   double vmin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(vstep <= 0.0)
      vstep = 0.01;
   lots = MathFloor(lots / vstep + 1e-9) * vstep;
   lots = MathMax(vmin, MathMin(vmax, lots));
   return NormalizeDouble(lots, 2);
  }

//+------------------------------------------------------------------+
//| この EA のポジション集計                                          |
//+------------------------------------------------------------------+
struct Basket
  {
   int    count;
   double volume;
   double avg_price;
   double lowest_price;
   double profit;      // 含み損益 (口座通貨, スワップ込み)
  };

Basket ReadBasket()
  {
   Basket b;
   b.count = 0;
   b.volume = 0.0;
   b.avg_price = 0.0;
   b.lowest_price = DBL_MAX;
   b.profit = 0.0;
   double sum_pv = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
         (ulong)PositionGetInteger(POSITION_MAGIC) != MagicNumber ||
         PositionGetInteger(POSITION_TYPE) != POSITION_TYPE_BUY)
         continue;
      double vol = PositionGetDouble(POSITION_VOLUME);
      double px  = PositionGetDouble(POSITION_PRICE_OPEN);
      b.count++;
      b.volume += vol;
      sum_pv   += vol * px;
      b.lowest_price = MathMin(b.lowest_price, px);
      b.profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   if(b.volume > 0.0)
      b.avg_price = sum_pv / b.volume;
   return b;
  }

//+------------------------------------------------------------------+
//| この EA のポジションを全決済                                      |
//+------------------------------------------------------------------+
void CloseAll(const string why)
  {
   PrintFormat("全決済: %s", why);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
         (ulong)PositionGetInteger(POSITION_MAGIC) != MagicNumber)
         continue;
      if(!g_trade.PositionClose(ticket))
         PrintFormat("決済失敗 #%I64u: %d %s", ticket, g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
     }
  }

//+------------------------------------------------------------------+
//| 1 段 (PositionsPerLevel 本) を成行買い                            |
//+------------------------------------------------------------------+
bool BuyLevel(const string comment)
  {
   double lots = NormalizeLots(Lots);
   bool ok = true;
   for(int k = 0; k < PositionsPerLevel; k++)
     {
      if(!g_trade.Buy(lots, _Symbol, 0.0, 0.0, 0.0, comment))
        {
         PrintFormat("買い失敗: %d %s", g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
         ok = false;
         break;
        }
     }
   return ok;
  }

//+------------------------------------------------------------------+
//| 確定 H1 終値が EMA より上か                                       |
//+------------------------------------------------------------------+
bool TrendOK()
  {
   if(!UseTrendFilter)
      return true;
   double ema[1];
   if(CopyBuffer(g_ema_handle, 0, 1, 1, ema) != 1)
      return false;
   double close_h1 = iClose(_Symbol, PERIOD_H1, 1);
   return close_h1 > 0.0 && close_h1 > ema[0];
  }

//+------------------------------------------------------------------+
//| 直近 LookbackMinutes 本 (確定足) の高値                           |
//+------------------------------------------------------------------+
double RecentHigh()
  {
   double highs[];
   int got = CopyHigh(_Symbol, PERIOD_M1, 1, LookbackMinutes, highs);
   if(got <= 0)
      return 0.0;
   return highs[ArrayMaximum(highs, 0, got)];
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(bid <= 0.0 || ask <= 0.0)
      return;
   bool spread_ok = (ask - bid) <= MaxSpreadUSD;

   Basket b = ReadBasket();

   //--- 保有中: 決済 → 買い増し
   if(b.count > 0)
     {
      if(bid >= b.avg_price + TakeProfitUSD)
        {
         CloseAll(StringFormat("利確 Bid %.2f >= 平均 %.2f + %.2f", bid, b.avg_price, TakeProfitUSD));
         return;
        }
      if(StopLossUSD > 0.0 && bid <= b.avg_price - StopLossUSD)
        {
         CloseAll(StringFormat("損切り Bid %.2f <= 平均 %.2f - %.2f", bid, b.avg_price, StopLossUSD));
         return;
        }
      if(AccountStopPercent > 0.0)
        {
         double balance = AccountInfoDouble(ACCOUNT_BALANCE);
         if(b.profit <= -balance * AccountStopPercent / 100.0)
           {
            CloseAll(StringFormat("口座ストップ 含み損 %.0f / 残高 %.0f", b.profit, balance));
            return;
           }
        }
      int levels = (b.count + PositionsPerLevel - 1) / PositionsPerLevel;
      if(levels < MaxLevels && spread_ok && ask <= b.lowest_price - GridStepUSD)
         BuyLevel(StringFormat("DipStack L%d", levels + 1));
      return;
     }

   //--- 未保有: 1 分足が確定したときだけ新規判定
   datetime bar = iTime(_Symbol, PERIOD_M1, 0);
   if(bar == 0 || bar == g_last_m1_bar)
      return;
   g_last_m1_bar = bar;
   if(!spread_ok)
      return;
   double hi = RecentHigh();
   if(hi <= 0.0)
      return;
   if(bid <= hi - DipUSD && TrendOK())
      BuyLevel("DipStack L1");
  }
//+------------------------------------------------------------------+
