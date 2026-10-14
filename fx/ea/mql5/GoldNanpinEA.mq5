//+------------------------------------------------------------------+
//|                                                GoldNanpinEA.mq5  |
//|  金(XAUUSD)向けナンピンEA                                         |
//|                                                                  |
//|  ・バスケットが空なら初期ロットでエントリー(トレンドフィルタ通過時)|
//|  ・直近エントリー価格からナンピン幅だけ逆行したら                 |
//|    初期ロット x 倍率^本数 のロットで追加 (最大本数まで)           |
//|  ・バスケットの合計損益(スワップ込み)が利確目標に達したら一括決済 |
//|  ・口座全体の損切り: EAポジションの含み損が残高のX%で全決済し、   |
//|    一定時間新規エントリーを停止                                   |
//|                                                                  |
//|  Python バックテスター (backtest/nanpin_bt.py) と同じロジック。   |
//|  ヘッジ口座 (両建て可能な口座) 専用。                             |
//+------------------------------------------------------------------+
#property copyright "razanohito"
#property version   "1.00"

#include <Trade\Trade.mqh>

enum ENUM_NANPIN_DIRECTION
  {
   DIR_BUY_ONLY  = 0, // 買いのみ
   DIR_SELL_ONLY = 1, // 売りのみ
   DIR_BOTH      = 2  // 両方 (買い・売りを独立管理)
  };

//--- 入力パラメータ (既定値 = results.md の候補A)
input group "売買設定"
input ENUM_NANPIN_DIRECTION InpDirection   = DIR_BOTH;  // 売買方向
input double               InpInitLot      = 0.10;      // 初期ロット
input double               InpMultiplier   = 1.2;       // ロット倍率
input double               InpGridStep     = 100.0;     // ナンピン幅 (価格, 金なら USD)
input int                  InpMaxPositions = 20;        // 最大本数 (片方向あたり)
input double               InpTPMove       = 20.0;      // 利確: 初期ロットがこの価格幅動いた分の利益で一括決済

input group "リスク管理"
input double               InpAccountStopPct = 5.0;     // 口座全体の損切り (残高比 %, 0=無効)
input int                  InpCooldownMin    = 1440;    // 損切り後の新規停止時間 (分)
input int                  InpMaxSpreadPoints = 0;      // 新規・ナンピン時の最大スプレッド (ポイント, 0=無制限)

input group "トレンドフィルタ (新規エントリーのみに適用)"
input ENUM_TIMEFRAMES      InpFilterTF     = PERIOD_H4; // フィルタの時間足
input int                  InpFilterMA     = 100;       // SMA 期間 (0=フィルタなし)

input group "その他"
input ulong                InpMagic        = 20261014;  // マジックナンバー
input int                  InpSlippage     = 30;        // 許容スリッページ (ポイント)

//--- グローバル
CTrade   g_trade;
int      g_ma_handle = INVALID_HANDLE;
string   g_gv_cooldown;

struct Basket
  {
   int      count;
   double   lots;
   double   profit;      // 損益 + スワップ
   double   last_price;  // 直近エントリーの約定価格
   long     last_msc;    // 直近エントリーの時刻 (ミリ秒)
  };

//+------------------------------------------------------------------+
int OnInit()
  {
   if((ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE) != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
     {
      Print("GoldNanpinEA: ヘッジ口座でのみ動作します (ネッティング口座ではナンピンのポジションが合算されるため)。");
      return(INIT_FAILED);
     }
   if(InpInitLot <= 0 || InpMultiplier <= 0 || InpGridStep <= 0 || InpMaxPositions < 1 || InpTPMove <= 0)
     {
      Print("GoldNanpinEA: 入力値が不正です。");
      return(INIT_PARAMETERS_INCORRECT);
     }
   if(InpFilterMA > 0)
     {
      g_ma_handle = iMA(_Symbol, InpFilterTF, InpFilterMA, 0, MODE_SMA, PRICE_CLOSE);
      if(g_ma_handle == INVALID_HANDLE)
        {
         Print("GoldNanpinEA: iMA の作成に失敗しました。 err=", GetLastError());
         return(INIT_FAILED);
        }
     }
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippage);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.SetAsyncMode(false);
   g_gv_cooldown = StringFormat("GoldNanpinEA_%s_%I64u_cooldown", _Symbol, InpMagic);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_ma_handle != INVALID_HANDLE)
      IndicatorRelease(g_ma_handle);
  }

//+------------------------------------------------------------------+
//| ロット計算: 初期ロット x 倍率^n をロットステップに丸める          |
//+------------------------------------------------------------------+
double CalcLot(const int n)
  {
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double vmin = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0)
      step = 0.01;
   double raw = InpInitLot * MathPow(InpMultiplier, n);
   double lot = MathFloor(raw / step + 0.5 + 1e-9) * step;
   if(lot < vmin)
      lot = vmin;
   if(lot > vmax)
      lot = vmax;
   int digits = (int)MathMax(0, MathCeil(-MathLog10(step)));
   return(NormalizeDouble(lot, digits));
  }

//+------------------------------------------------------------------+
//| 利確目標 (口座通貨): 初期ロットが InpTPMove 動いた時の利益        |
//+------------------------------------------------------------------+
double TPMoney()
  {
   double price = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   double profit = 0.0;
   if(!OrderCalcProfit(ORDER_TYPE_BUY, _Symbol, CalcLot(0), price, price + InpTPMove, profit))
     {
      // 失敗時はティック値から概算
      double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
      double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
      if(ts > 0)
         profit = CalcLot(0) * InpTPMove / ts * tv;
     }
   return(profit);
  }

//+------------------------------------------------------------------+
void ReadBaskets(Basket &buy, Basket &sell)
  {
   ZeroMemory(buy);
   ZeroMemory(sell);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      double   vol  = PositionGetDouble(POSITION_VOLUME);
      double   pnl  = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      double   open = PositionGetDouble(POSITION_PRICE_OPEN);
      long     tmsc = PositionGetInteger(POSITION_TIME_MSC);
      if(PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY)
        {
         buy.count++;
         buy.lots   += vol;
         buy.profit += pnl;
         if(buy.count == 1 || tmsc >= buy.last_msc)
           {
            buy.last_msc   = tmsc;
            buy.last_price = open;
           }
        }
      else
        {
         sell.count++;
         sell.lots   += vol;
         sell.profit += pnl;
         if(sell.count == 1 || tmsc >= sell.last_msc)
           {
            sell.last_msc   = tmsc;
            sell.last_price = open;
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| 指定方向のEAポジションを全決済 (type<0 なら全方向)               |
//+------------------------------------------------------------------+
bool CloseBasket(const int type)
  {
   bool ok = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      if(type >= 0 && PositionGetInteger(POSITION_TYPE) != type)
         continue;
      if(!g_trade.PositionClose(ticket, InpSlippage))
        {
         PrintFormat("GoldNanpinEA: 決済失敗 ticket=%I64u retcode=%u %s", ticket,
                     g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
         ok = false;
        }
     }
   return(ok);
  }

//+------------------------------------------------------------------+
//| トレンドフィルタ: +1 上昇 (買い許可) / -1 下降 (売り許可) / 0 不明 |
//+------------------------------------------------------------------+
int TrendState()
  {
   if(InpFilterMA <= 0)
      return(2); // フィルタなし = 常に許可
   double ma[1];
   if(CopyBuffer(g_ma_handle, 0, 1, 1, ma) != 1)
      return(0);
   double cl = iClose(_Symbol, InpFilterTF, 1);
   if(cl <= 0 || ma[0] <= 0)
      return(0);
   if(cl > ma[0])
      return(1);
   if(cl < ma[0])
      return(-1);
   return(0);
  }

//+------------------------------------------------------------------+
bool SpreadOK()
  {
   if(InpMaxSpreadPoints <= 0)
      return(true);
   return(SymbolInfoInteger(_Symbol, SYMBOL_SPREAD) <= InpMaxSpreadPoints);
  }

//+------------------------------------------------------------------+
datetime CooldownUntil()
  {
   if(GlobalVariableCheck(g_gv_cooldown))
      return((datetime)GlobalVariableGet(g_gv_cooldown));
   return(0);
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return;

   Basket buy, sell;
   ReadBaskets(buy, sell);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);

   //--- 1. 口座全体の損切り
   if(InpAccountStopPct > 0 && (buy.count + sell.count) > 0)
     {
      double floating = buy.profit + sell.profit;
      if(floating <= -balance * InpAccountStopPct / 100.0)
        {
         PrintFormat("GoldNanpinEA: 口座損切り 含み損=%.0f 残高=%.0f", floating, balance);
         CloseBasket(-1);
         datetime until = TimeCurrent() + InpCooldownMin * 60;
         GlobalVariableSet(g_gv_cooldown, (double)until);
         return;
        }
     }

   //--- 2. バスケット利確
   double tp = TPMoney();
   if(buy.count > 0 && buy.profit >= tp)
     {
      if(CloseBasket(POSITION_TYPE_BUY))
         buy.count = 0;
     }
   if(sell.count > 0 && sell.profit >= tp)
     {
      if(CloseBasket(POSITION_TYPE_SELL))
         sell.count = 0;
     }

   double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   //--- 3. ナンピン
   if(buy.count > 0 && buy.count < InpMaxPositions && ask <= buy.last_price - InpGridStep && SpreadOK())
     {
      double lot = CalcLot(buy.count);
      if(!g_trade.Buy(lot, _Symbol, 0, 0, 0, StringFormat("nanpin buy #%d", buy.count + 1)))
         PrintFormat("GoldNanpinEA: 買いナンピン失敗 retcode=%u", g_trade.ResultRetcode());
     }
   if(sell.count > 0 && sell.count < InpMaxPositions && bid >= sell.last_price + InpGridStep && SpreadOK())
     {
      double lot = CalcLot(sell.count);
      if(!g_trade.Sell(lot, _Symbol, 0, 0, 0, StringFormat("nanpin sell #%d", sell.count + 1)))
         PrintFormat("GoldNanpinEA: 売りナンピン失敗 retcode=%u", g_trade.ResultRetcode());
     }

   //--- 4. 新規エントリー
   if(TimeCurrent() < CooldownUntil() || !SpreadOK())
      return;
   int trend = TrendState();
   bool allow_buy  = (InpDirection == DIR_BUY_ONLY  || InpDirection == DIR_BOTH) && (trend == 2 || trend == 1);
   bool allow_sell = (InpDirection == DIR_SELL_ONLY || InpDirection == DIR_BOTH) && (trend == 2 || trend == -1);
   if(allow_buy && buy.count == 0)
     {
      if(!g_trade.Buy(CalcLot(0), _Symbol, 0, 0, 0, "nanpin buy #1"))
         PrintFormat("GoldNanpinEA: 買いエントリー失敗 retcode=%u", g_trade.ResultRetcode());
     }
   if(allow_sell && sell.count == 0)
     {
      if(!g_trade.Sell(CalcLot(0), _Symbol, 0, 0, 0, "nanpin sell #1"))
         PrintFormat("GoldNanpinEA: 売りエントリー失敗 retcode=%u", g_trade.ResultRetcode());
     }
  }
//+------------------------------------------------------------------+
