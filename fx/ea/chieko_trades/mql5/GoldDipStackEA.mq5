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
//|     TrendFilter で上昇トレンドのときだけに絞る                     |
//|     (H4 SMA100 = 確定 H4 終値 > H4 SMA100, H1 EMA = 確定 H1 終値 > EMA)。|
//|  2) 一番安い建値から GridStepUSD 下げるごとに同じロットで 1 段追加 |
//|     (最大 MaxLevels 段。ロットは増やさない)。                      |
//|  3) Bid >= 平均建値 + TakeProfitUSD で全決済。                     |
//|     StopLossUSD > 0 なら Bid <= 平均建値 - StopLossUSD で全決済。  |
//|     MaxHoldHours > 0 なら 1 本目からその時間で全決済 (塩漬け防止)。|
//|     AccountStopPercent > 0 なら含み損が残高の X% で全決済。        |
//|  4) 損切り・口座ストップ・損失での時間切れの後は                   |
//|     CooldownHours の間、新規エントリーしない。                     |
//|                                                                  |
//|  初期値は optimize.py の探索 (151,200 通り) で選んだもの:          |
//|  0.01 ロット x 3 本/段で 2026-04-20〜09-25 に +10.1 万円,           |
//|  最大DD 4.3 万円, 8 週間 x 15 期間すべて黒字。results/optimize.md。|
//|  実口座で使う前に必ずデモとストラテジーテスターで確認すること。    |
//+------------------------------------------------------------------+
#property copyright "razanohito/development"
#property version   "1.10"

#include <Trade\Trade.mqh>

enum ENUM_TREND_FILTER
  {
   TREND_NONE   = 0,   // なし
   TREND_H1_EMA = 1,   // 確定 H1 終値 > H1 EMA
   TREND_H4_SMA = 2    // 確定 H4 終値 > H4 SMA
  };

//--- 入力パラメータ
input group "ロット"
input double Lots               = 0.01;   // 1 本のロット (履歴は 0.50)
input int    PositionsPerLevel  = 3;      // 1 段で買う本数

input group "エントリー"
input int    LookbackMinutes    = 480;    // 押し目判定に使う直近 1 分足の本数
input double DipUSD             = 60.0;   // 直近高値からこのドル数下げたら買う
input ENUM_TREND_FILTER TrendFilter = TREND_H4_SMA; // 上昇トレンドのときだけ買う
input int    TrendEMAPeriod     = 50;     // H1 EMA の期間 (TREND_H1_EMA)
input int    TrendSMAPeriod     = 100;    // H4 SMA の期間 (TREND_H4_SMA)
input double MaxSpreadUSD       = 0.60;   // これより広いスプレッドでは新規・追加しない

input group "買い下がり"
input double GridStepUSD        = 15.0;   // 一番安い建値からこのドル数下げたら 1 段追加
input int    MaxLevels          = 2;      // 最大段数 (1 = 買い下がりなし)

input group "決済"
input double TakeProfitUSD      = 3.0;    // 平均建値 + このドル数で全決済
input double StopLossUSD        = 40.0;   // 平均建値 - このドル数で全決済 (0 = なし)
input double MaxHoldHours       = 24.0;   // 1 本目からこの時間が過ぎたら全決済 (0 = なし)
input double AccountStopPercent = 0.0;    // 含み損が残高のこの % に達したら全決済 (0 = なし)
input double CooldownHours      = 24.0;   // 損切り・損失決済の後、新規エントリーを休む時間 (0 = なし)

input group "その他"
input ulong  MagicNumber        = 20261020;
input int    SlippagePoints     = 50;

//--- グローバル
CTrade   g_trade;
int      g_ma_handle = INVALID_HANDLE;
datetime g_last_m1_bar = 0;
string   g_cooldown_key;

//+------------------------------------------------------------------+
int OnInit()
  {
   if(Lots <= 0.0 || PositionsPerLevel < 1 || MaxLevels < 1 || LookbackMinutes < 1 ||
      DipUSD < 0.0 || GridStepUSD <= 0.0 || TakeProfitUSD <= 0.0 || StopLossUSD < 0.0 ||
      MaxHoldHours < 0.0 || AccountStopPercent < 0.0 || CooldownHours < 0.0 ||
      TrendEMAPeriod < 1 || TrendSMAPeriod < 1)
     {
      Print("入力値が不正です");
      return INIT_PARAMETERS_INCORRECT;
     }
   g_trade.SetExpertMagicNumber(MagicNumber);
   g_trade.SetDeviationInPoints(SlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   if(TrendFilter == TREND_H1_EMA)
      g_ma_handle = iMA(_Symbol, PERIOD_H1, TrendEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   else
      if(TrendFilter == TREND_H4_SMA)
         g_ma_handle = iMA(_Symbol, PERIOD_H4, TrendSMAPeriod, 0, MODE_SMA, PRICE_CLOSE);
   if(TrendFilter != TREND_NONE && g_ma_handle == INVALID_HANDLE)
     {
      Print("トレンドフィルタの移動平均を作成できません: ", GetLastError());
      return INIT_FAILED;
     }
   // クールダウンの終了時刻は再起動しても残るようにグローバル変数に保存する
   g_cooldown_key = StringFormat("GoldDipStackEA_%s_%I64u_resume", _Symbol, MagicNumber);
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_ma_handle != INVALID_HANDLE)
      IndicatorRelease(g_ma_handle);
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
   int      count;
   double   volume;
   double   avg_price;
   double   lowest_price;
   double   profit;      // 含み損益 (口座通貨, スワップ込み)
   datetime first_time;  // 一番古いポジションの約定時刻
  };

Basket ReadBasket()
  {
   Basket b;
   b.count = 0;
   b.volume = 0.0;
   b.avg_price = 0.0;
   b.lowest_price = DBL_MAX;
   b.profit = 0.0;
   b.first_time = 0;
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
      double   vol = PositionGetDouble(POSITION_VOLUME);
      double   px  = PositionGetDouble(POSITION_PRICE_OPEN);
      datetime t   = (datetime)PositionGetInteger(POSITION_TIME);
      b.count++;
      b.volume += vol;
      sum_pv   += vol * px;
      b.lowest_price = MathMin(b.lowest_price, px);
      b.profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      if(b.first_time == 0 || t < b.first_time)
         b.first_time = t;
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
//| 損失で決済したあとのクールダウン                                  |
//+------------------------------------------------------------------+
void StartCooldown()
  {
   if(CooldownHours <= 0.0)
      return;
   datetime resume = TimeCurrent() + (datetime)(CooldownHours * 3600.0);
   GlobalVariableSet(g_cooldown_key, (double)resume);
   PrintFormat("新規エントリーを %s まで休みます", TimeToString(resume, TIME_DATE | TIME_MINUTES));
  }

bool InCooldown()
  {
   if(CooldownHours <= 0.0 || !GlobalVariableCheck(g_cooldown_key))
      return false;
   return TimeCurrent() < (datetime)GlobalVariableGet(g_cooldown_key);
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
//| 確定した上位足の終値が移動平均より上か                            |
//+------------------------------------------------------------------+
bool TrendOK()
  {
   if(TrendFilter == TREND_NONE)
      return true;
   ENUM_TIMEFRAMES tf = (TrendFilter == TREND_H1_EMA) ? PERIOD_H1 : PERIOD_H4;
   double ma[1];
   if(CopyBuffer(g_ma_handle, 0, 1, 1, ma) != 1)
      return false;
   double close_tf = iClose(_Symbol, tf, 1);
   return close_tf > 0.0 && close_tf > ma[0];
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
         StartCooldown();
         return;
        }
      if(AccountStopPercent > 0.0)
        {
         double balance = AccountInfoDouble(ACCOUNT_BALANCE);
         if(b.profit <= -balance * AccountStopPercent / 100.0)
           {
            CloseAll(StringFormat("口座ストップ 含み損 %.0f / 残高 %.0f", b.profit, balance));
            StartCooldown();
            return;
           }
        }
      if(MaxHoldHours > 0.0 && TimeCurrent() - b.first_time >= (datetime)(MaxHoldHours * 3600.0))
        {
         CloseAll(StringFormat("時間切れ %.0f 時間 (含み損益 %.0f)", MaxHoldHours, b.profit));
         if(b.profit < 0.0)
            StartCooldown();
         return;
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
   if(!spread_ok || InCooldown())
      return;
   double hi = RecentHigh();
   if(hi <= 0.0)
      return;
   if(bid <= hi - DipUSD && TrendOK())
      BuyLevel("DipStack L1");
  }
//+------------------------------------------------------------------+
