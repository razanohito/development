//+------------------------------------------------------------------+
//|                                          TrendNanpinHedgeEA.mq5  |
//|  順張りナンピン (ロジック1) + 逆方向ポジションでの救済 (ロジック2) |
//|  tnh_bt.py / study_risk.py (Python バックテスト) と同じルール。    |
//|  MT4 版は mql4/TrendNanpinHedgeEA.mq4。                            |
//|                                                                  |
//|  1) 初回エントリー: 5 分足の CCI(14) が 0 を上抜けたら買い、       |
//|     下抜けたら売り (確定足で判定)。InpAdxMax > 0 なら H1 の ADX が |
//|     その値より大きい強トレンド中は入らない。                       |
//|     ほかに RSI 押し目・ブレイク・外部シグナル (AI がグローバル変数 |
//|     TNH_SIGNAL_<シンボル> に +1 / -1) も選べる。                   |
//|  2) ロジック1 順張りナンピン: 直前のエントリー位置から             |
//|     ナンピン間隔だけ逆行するごとに次の 1 本 (最大 InpMaxPositions)。|
//|     ロット = InpLot0 × InpLotMult^(n-1) を刻みに丸める。           |
//|  3) ロジック2 救済: 3 本目と 4 本目は 1 本目と逆方向 (5 本目なし)。|
//|     逆方向ポジションには 間隔×InpHedgeTPRatio の利確を付ける。     |
//|  4) 決済: サイクル損益 (救済の確定利益 + 含み損益) が               |
//|     間隔×InpBasketTPRatio×InpLot0 相当に達したら全決済。            |
//|     4 本持ったら、最も逆行した位置から 間隔×InpRevRatio 戻った     |
//|     時点で全決済 (反転で即決済)。                                   |
//|  5) 大負け対策: 1 サイクルの損失が InpMaxLossMoney (口座通貨) に   |
//|     達したら全決済。                                                |
//|                                                                  |
//|  初期値は「バランス型」(固定 300pips, 損失上限 3 万円)。            |
//|  「守り重視」は TrendNanpinHedge_defensive.set。                   |
//|  金 (XAU / GOLD) の 1 pip は 0.1 ドルとして扱う (InpPipSize で変更可)。|
//|  検証結果は results/compare_risk.md。はっきりした優位性は確認でき  |
//|  ていないので、実口座の前に必ずデモで確認すること。                |
//+------------------------------------------------------------------+
#property copyright "razanohito/development"
#property version   "1.10"

#include <Trade\Trade.mqh>

enum ENUM_STEP_MODE
  {
   STEP_FIXED_PIPS = 0, // 固定 pips
   STEP_DAILY_ATR  = 1  // 日足 ATR × 係数
  };

enum ENUM_AFTER_HEDGE
  {
   AFTER_NANPIN_ORIGINAL = 0, // 4 本目以降は元の方向にナンピン
   AFTER_KEEP_REVERSE    = 1  // 4 本目以降も逆方向 (救済を続ける)
  };

enum ENUM_ENTRY_MODE
  {
   ENTRY_RSI_PULLBACK = 0, // EMA の向きに RSI 押し目
   ENTRY_BREAKOUT     = 1, // EMA の向きに 高値/安値ブレイク
   ENTRY_EXTERNAL     = 2, // 外部シグナル (AI がグローバル変数に +1 / -1)
   ENTRY_CCI_ZERO     = 3  // CCI が 0 を上抜けで買い / 下抜けで売り
  };

//--- 入力パラメータ
input group "ロット・本数"
input double InpLot0          = 0.01;  // 1 本目のロット
input double InpLotMult       = 1.5;   // ナンピン倍率
input int    InpMaxPositions  = 4;     // 1 サイクルの最大本数 (5 本目はなし)

input group "ナンピン間隔"
input ENUM_STEP_MODE InpStepMode = STEP_FIXED_PIPS; // 間隔の決め方
input double InpStepPips      = 300.0; // 固定のときの間隔 (pips。金 1pip=0.1 ドルなので 30 ドル)
input double InpStepAtrK      = 0.5;   // ATR のときの係数 (間隔 = 日足 ATR × 係数)
input int    InpAtrPeriod     = 14;    // 日足 ATR の期間
input double InpPipSize       = 0.0;   // 1 pip の価格幅 (0 = 自動: 金 0.1, 3/5 桁は point×10)

input group "ロジック2 (逆方向で救済)"
input int    InpHedgeFrom     = 3;     // 何本目から逆方向にするか (0 = 救済しない)
input ENUM_AFTER_HEDGE InpAfterHedge = AFTER_KEEP_REVERSE; // 救済の後 (4 本目以降) の向き
input double InpHedgeTPRatio  = 2.0;   // 逆方向ポジションの利確 = 間隔 × この比率

input group "決済"
input double InpBasketTPRatio = 1.0;   // サイクル利確 = 間隔 × この比率 × 1 本目のロット相当
input int    InpRevFrom       = 4;     // この本数以上で「反転したら即決済」(0 = 使わない)
input double InpRevRatio      = 0.5;   // 反転とみなす戻り幅 = 間隔 × この比率
input double InpMaxLossMoney  = 30000.0; // 1 サイクルの損失がこの金額 (口座通貨) で全決済 (0 = なし。円口座 30000 ≒ ドル口座 200)

input group "初回エントリー (AI の代わりのルール)"
input ENUM_ENTRY_MODE InpEntryMode = ENTRY_CCI_ZERO;     // 初回エントリーの決め方
input ENUM_TIMEFRAMES InpSignalTF  = PERIOD_M5;          // シグナルを見る時間足
input int    InpCciPeriod     = 14;    // CCI 期間 (典型価格)
input int    InpEmaPeriod     = 200;   // トレンド判定の EMA 期間
input int    InpRsiPeriod     = 14;    // RSI 期間
input double InpRsiBuyLevel   = 40.0;  // 上昇トレンドで RSI がこの値を下から上に抜けたら買い
input double InpRsiSellLevel  = 60.0;  // 下降トレンドで RSI がこの値を上から下に抜けたら売り
input int    InpBreakoutBars  = 20;    // ブレイクに使う直近の本数
input string InpSignalGVName  = "";    // 外部シグナルのグローバル変数名 (空 = TNH_SIGNAL_<シンボル>)
input bool   InpAllowBuy      = true;  // 買いから始めるサイクルを許可
input bool   InpAllowSell     = true;  // 売りから始めるサイクルを許可
input double InpAdxMax        = 0.0;   // 強トレンド停止: ADX がこの値より大きいときは新規に入らない (0 = なし, 守り重視は 25)
input ENUM_TIMEFRAMES InpAdxTF     = PERIOD_H1;          // ADX を見る時間足
input int    InpAdxPeriod     = 14;    // ADX 期間
input double InpMaxSpreadPips = 0.0;   // これより広いスプレッドでは建てない (0 = 制限なし)

input group "その他"
input ulong  InpMagic         = 20261102;
input int    InpSlippagePoints = 50;

//--- グローバル
CTrade   g_trade;
int      g_ema = INVALID_HANDLE;
int      g_rsi = INVALID_HANDLE;
int      g_cci = INVALID_HANDLE;
int      g_adx = INVALID_HANDLE;
int      g_atr = INVALID_HANDLE;
double   g_pip = 0.0;
datetime g_last_sig_bar = 0;
string   g_prefix;
string   g_sig_gv;
double   g_realized = 0.0;   // サイクル開始後の確定損益 (キャッシュ)
int      g_last_npos = -1;

//--- サイクルの状態 (再起動しても続けられるようにグローバル変数にも保存)
int      c_dir      = 0;    // 1 本目の方向 (+1 買い / -1 売り, 0 = サイクルなし)
int      c_nlev     = 0;    // このサイクルで建てた本数
double   c_ref      = 0.0;  // 直前のエントリー位置 (Bid)
double   c_step     = 0.0;  // ナンピン間隔 (価格)
double   c_extreme  = 0.0;  // サイクル開始後の最も逆行した Bid
datetime c_start    = 0;    // サイクル開始時刻

//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpLot0 <= 0.0 || InpLotMult <= 0.0 || InpMaxPositions < 1 || InpStepPips <= 0.0 ||
      InpStepAtrK <= 0.0 || InpAtrPeriod < 1 || InpHedgeFrom < 0 || InpHedgeTPRatio <= 0.0 ||
      InpBasketTPRatio <= 0.0 || InpRevFrom < 0 || InpRevRatio <= 0.0 || InpMaxLossMoney < 0.0 ||
      InpEmaPeriod < 1 || InpRsiPeriod < 1 || InpBreakoutBars < 1 || InpCciPeriod < 2 ||
      InpAdxMax < 0.0 || InpAdxPeriod < 1)
     {
      Print("入力値が不正です");
      return INIT_PARAMETERS_INCORRECT;
     }
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   g_trade.SetTypeFillingBySymbol(_Symbol);

   g_pip = PipSize();
   g_ema = iMA(_Symbol, InpSignalTF, InpEmaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   g_rsi = iRSI(_Symbol, InpSignalTF, InpRsiPeriod, PRICE_CLOSE);
   g_atr = iATR(_Symbol, PERIOD_D1, InpAtrPeriod);
   g_cci = iCCI(_Symbol, InpSignalTF, InpCciPeriod, PRICE_TYPICAL);
   g_adx = iADX(_Symbol, InpAdxTF, InpAdxPeriod);
   if(g_ema == INVALID_HANDLE || g_rsi == INVALID_HANDLE || g_atr == INVALID_HANDLE ||
      g_cci == INVALID_HANDLE || g_adx == INVALID_HANDLE)
     {
      Print("インジケーターを作成できません: ", GetLastError());
      return INIT_FAILED;
     }
   g_prefix = StringFormat("TNH_%s_%I64u_", _Symbol, InpMagic);
   g_sig_gv = (InpSignalGVName == "") ? "TNH_SIGNAL_" + _Symbol : InpSignalGVName;
   LoadState();
   PrintFormat("TrendNanpinHedgeEA 開始: 1pip=%.5f, 間隔=%s", g_pip,
               InpStepMode == STEP_FIXED_PIPS ? DoubleToString(InpStepPips, 1) + "pips"
               : "日足ATR×" + DoubleToString(InpStepAtrK, 2));
   return INIT_SUCCEEDED;
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   if(g_ema != INVALID_HANDLE) IndicatorRelease(g_ema);
   if(g_rsi != INVALID_HANDLE) IndicatorRelease(g_rsi);
   if(g_cci != INVALID_HANDLE) IndicatorRelease(g_cci);
   if(g_adx != INVALID_HANDLE) IndicatorRelease(g_adx);
   if(g_atr != INVALID_HANDLE) IndicatorRelease(g_atr);
   Comment("");
  }

//+------------------------------------------------------------------+
//| 1 pip の価格幅                                                    |
//+------------------------------------------------------------------+
double PipSize()
  {
   if(InpPipSize > 0.0)
      return InpPipSize;
   string s = _Symbol;
   StringToUpper(s);
   if(StringFind(s, "XAU") >= 0 || StringFind(s, "GOLD") >= 0)
      return 0.1;
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double pt = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   if(digits == 3 || digits == 5)
      return pt * 10.0;
   return pt;
  }

//+------------------------------------------------------------------+
//| 状態の保存・読み込み                                              |
//+------------------------------------------------------------------+
void SaveState()
  {
   GlobalVariableSet(g_prefix + "dir", c_dir);
   GlobalVariableSet(g_prefix + "nlev", c_nlev);
   GlobalVariableSet(g_prefix + "ref", c_ref);
   GlobalVariableSet(g_prefix + "step", c_step);
   GlobalVariableSet(g_prefix + "extreme", c_extreme);
   GlobalVariableSet(g_prefix + "start", (double)c_start);
  }

void LoadState()
  {
   c_dir = 0;
   if(!GlobalVariableCheck(g_prefix + "dir"))
      return;
   c_dir     = (int)GlobalVariableGet(g_prefix + "dir");
   c_nlev    = (int)GlobalVariableGet(g_prefix + "nlev");
   c_ref     = GlobalVariableGet(g_prefix + "ref");
   c_step    = GlobalVariableGet(g_prefix + "step");
   c_extreme = GlobalVariableGet(g_prefix + "extreme");
   c_start   = (datetime)(long)GlobalVariableGet(g_prefix + "start");
   if(c_dir != 0 && CountPositions() == 0)
      ResetState();  // 停止中に全部閉じられていた
   if(c_dir != 0)
      PrintFormat("サイクルを再開: 方向=%d 本数=%d 間隔=%.2f", c_dir, c_nlev, c_step);
  }

void ResetState()
  {
   g_realized = 0.0;
   g_last_npos = -1;
   c_dir = 0;
   c_nlev = 0;
   c_ref = 0.0;
   c_step = 0.0;
   c_extreme = 0.0;
   c_start = 0;
   GlobalVariableSet(g_prefix + "dir", 0);
  }

//+------------------------------------------------------------------+
//| ロットをシンボルの刻みに丸める (四捨五入)                         |
//+------------------------------------------------------------------+
double NormalizeLots(double lots)
  {
   double vmin  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double vmax  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double vstep = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(vstep <= 0.0)
      vstep = 0.01;
   double n = MathFloor(lots / vstep + 0.5);
   if(n < 1.0)
      n = 1.0;
   lots = n * vstep;
   lots = MathMax(vmin, MathMin(vmax, lots));
   return NormalizeDouble(lots, 2);
  }

//+------------------------------------------------------------------+
//| 価格差 × ロット → 口座通貨                                        |
//+------------------------------------------------------------------+
double MoneyFor(double price_diff, double lots)
  {
   double tick_size  = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   if(tick_size <= 0.0)
      return 0.0;
   return price_diff / tick_size * tick_value * lots;
  }

//+------------------------------------------------------------------+
//| この EA のポジション                                              |
//+------------------------------------------------------------------+
int CountPositions()
  {
   int n = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      n++;
     }
   return n;
  }

double FloatingProfit()
  {
   double p = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      p += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
     }
   return p;
  }

//+------------------------------------------------------------------+
//| サイクル開始後に決済済みの損益 (救済ポジションの利確など)         |
//+------------------------------------------------------------------+
double RealizedSinceStart()
  {
   if(c_start == 0)
      return 0.0;
   if(!HistorySelect(c_start, TimeCurrent() + 60))
      return 0.0;
   double p = 0.0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0) continue;
      if(HistoryDealGetString(d, DEAL_SYMBOL) != _Symbol) continue;
      if((ulong)HistoryDealGetInteger(d, DEAL_MAGIC) != InpMagic) continue;
      ENUM_DEAL_ENTRY e = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(d, DEAL_ENTRY);
      if(e != DEAL_ENTRY_OUT && e != DEAL_ENTRY_INOUT && e != DEAL_ENTRY_OUT_BY) continue;
      p += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP) +
           HistoryDealGetDouble(d, DEAL_COMMISSION);
     }
   return p;
  }

//+------------------------------------------------------------------+
//| 全決済                                                            |
//+------------------------------------------------------------------+
bool CloseAll(const string reason)
  {
   bool ok = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol) continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;
      if(!g_trade.PositionClose(t))
        {
         PrintFormat("決済に失敗 #%I64u: %d %s", t, g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
         ok = false;
        }
     }
   if(ok)
     {
      PrintFormat("全決済 (%s): 本数=%d", reason, c_nlev);
      ResetState();
     }
   return ok;
  }

//+------------------------------------------------------------------+
//| スプレッド確認                                                    |
//+------------------------------------------------------------------+
bool SpreadOK()
  {
   if(InpMaxSpreadPips <= 0.0)
      return true;
   double spr = SymbolInfoDouble(_Symbol, SYMBOL_ASK) - SymbolInfoDouble(_Symbol, SYMBOL_BID);
   return spr <= InpMaxSpreadPips * g_pip;
  }

//+------------------------------------------------------------------+
//| 1 本建てる。dir=+1 買い / -1 売り。tp_dist>0 なら利確を付ける      |
//+------------------------------------------------------------------+
bool OpenPosition(int dir, double lots, double tp_dist, int level)
  {
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   string cmt = StringFormat("TNH L%d", level);
   bool ok;
   if(dir > 0)
     {
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double tp = (tp_dist > 0.0) ? NormalizeDouble(ask + tp_dist, digits) : 0.0;
      ok = g_trade.Buy(lots, _Symbol, 0.0, 0.0, tp, cmt);
     }
   else
     {
      double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double tp = (tp_dist > 0.0) ? NormalizeDouble(bid - tp_dist, digits) : 0.0;
      ok = g_trade.Sell(lots, _Symbol, 0.0, 0.0, tp, cmt);
     }
   if(!ok || (g_trade.ResultRetcode() != TRADE_RETCODE_DONE && g_trade.ResultRetcode() != TRADE_RETCODE_PLACED))
     {
      PrintFormat("発注に失敗 L%d: %d %s", level, g_trade.ResultRetcode(), g_trade.ResultRetcodeDescription());
      return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
//| 強トレンド (確定足の ADX > InpAdxMax) なら true                    |
//+------------------------------------------------------------------+
bool StrongTrend()
  {
   if(InpAdxMax <= 0.0)
      return false;
   double adx[1];
   if(CopyBuffer(g_adx, 0, 1, 1, adx) != 1)
      return true;  // 取れないときは入らない
   return adx[0] > InpAdxMax;
  }

//+------------------------------------------------------------------+
//| 初回エントリーのシグナル (確定足で判定)                           |
//+------------------------------------------------------------------+
int EntrySignal()
  {
   if(InpEntryMode == ENTRY_EXTERNAL)
     {
      if(!GlobalVariableCheck(g_sig_gv))
         return 0;
      double v = GlobalVariableGet(g_sig_gv);
      if(v == 0.0)
         return 0;
      GlobalVariableSet(g_sig_gv, 0.0);  // 使ったら消す
      return v > 0.0 ? 1 : -1;
     }
   // ルールは SignalTF の新しい足ができたときだけ判定する
   datetime bar = iTime(_Symbol, InpSignalTF, 0);
   if(bar == 0 || bar == g_last_sig_bar)
      return 0;
   if(InpEntryMode == ENTRY_CCI_ZERO)
     {
      double cc[2];
      if(CopyBuffer(g_cci, 0, 1, 2, cc) != 2)  // cc[0]=2 本前, cc[1]=1 本前
         return 0;
      g_last_sig_bar = bar;
      if(cc[0] < 0.0 && cc[1] >= 0.0)
         return 1;
      if(cc[0] > 0.0 && cc[1] <= 0.0)
         return -1;
      return 0;
     }
   double ema[1], cls[1];
   if(CopyBuffer(g_ema, 0, 1, 1, ema) != 1 || CopyClose(_Symbol, InpSignalTF, 1, 1, cls) != 1)
      return 0;
   g_last_sig_bar = bar;
   if(Bars(_Symbol, InpSignalTF) < InpEmaPeriod + 2)
      return 0;
   bool up = cls[0] > ema[0];
   if(InpEntryMode == ENTRY_RSI_PULLBACK)
     {
      double r[2];
      if(CopyBuffer(g_rsi, 0, 1, 2, r) != 2)  // r[0]=2 本前, r[1]=1 本前
         return 0;
      if(up && r[0] < InpRsiBuyLevel && r[1] >= InpRsiBuyLevel)
         return 1;
      if(!up && r[0] > InpRsiSellLevel && r[1] <= InpRsiSellLevel)
         return -1;
      return 0;
     }
   // ブレイク: 1 本前の終値が、その前 InpBreakoutBars 本の高値 (安値) を抜けた
   double hi[], lo[];
   if(CopyHigh(_Symbol, InpSignalTF, 2, InpBreakoutBars, hi) != InpBreakoutBars ||
      CopyLow(_Symbol, InpSignalTF, 2, InpBreakoutBars, lo) != InpBreakoutBars)
      return 0;
   double hh = hi[ArrayMaximum(hi)];
   double ll = lo[ArrayMinimum(lo)];
   if(up && cls[0] > hh)
      return 1;
   if(!up && cls[0] < ll)
      return -1;
   return 0;
  }

//+------------------------------------------------------------------+
//| サイクル開始時のナンピン間隔 (価格)                               |
//+------------------------------------------------------------------+
double CurrentStep()
  {
   if(InpStepMode == STEP_FIXED_PIPS)
      return InpStepPips * g_pip;
   double atr[1];
   if(CopyBuffer(g_atr, 0, 1, 1, atr) != 1 || atr[0] <= 0.0)
      return 0.0;  // ATR が取れないときは入らない
   return atr[0] * InpStepAtrK;
  }

//+------------------------------------------------------------------+
//| n 本目の方向                                                      |
//+------------------------------------------------------------------+
int LevelDir(int level)
  {
   if(InpHedgeFrom >= 1 && level >= InpHedgeFrom)
     {
      if(level == InpHedgeFrom || InpAfterHedge == AFTER_KEEP_REVERSE)
         return -c_dir;
     }
   return c_dir;
  }

//+------------------------------------------------------------------+
void OnTick()
  {
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(bid <= 0.0)
      return;

   //--- サイクルなし: 初回エントリー
   if(c_dir == 0)
     {
      if(!SpreadOK())
        {
         ShowStatus(bid);
         return;
        }
      int sig = EntrySignal();
      if(sig != 0 && StrongTrend())
        {
         sig = 0;  // 強いトレンドの最中は入らない
        }
      if(sig == 0 || (sig > 0 && !InpAllowBuy) || (sig < 0 && !InpAllowSell))
        {
         ShowStatus(bid);
         return;
        }
      double step = CurrentStep();
      if(step <= 0.0)
         return;
      c_dir = sig;
      c_step = step;
      c_start = TimeCurrent();
      double lots = NormalizeLots(InpLot0);
      if(!OpenPosition(c_dir, lots, 0.0, 1))
        {
         c_dir = 0;
         return;
        }
      c_nlev = 1;
      c_ref = bid;
      c_extreme = bid;
      g_realized = 0.0;
      g_last_npos = -1;
      SaveState();
      PrintFormat("サイクル開始: %s 間隔=%.2f (%.1fpips)", c_dir > 0 ? "買い" : "売り", c_step, c_step / g_pip);
      return;
     }

   //--- 手動・ロスカットなどで全部閉じられたらサイクル終了
   int npos = CountPositions();
   if(npos == 0)
     {
      Print("ポジションが外部で全部決済されたのでサイクルを終了");
      ResetState();
      return;
     }
   // 確定損益は本数が変わったときだけ履歴から数え直す
   if(npos != g_last_npos)
     {
      g_realized = RealizedSinceStart();
      g_last_npos = npos;
     }

   //--- 最も逆行した位置を更新
   if((bid - c_extreme) * c_dir < 0.0)
     {
      c_extreme = bid;
      GlobalVariableSet(g_prefix + "extreme", c_extreme);
     }

   //--- サイクル損益で利確 / 安全装置
   double net = g_realized + FloatingProfit();
   double tp_money = MoneyFor(c_step * InpBasketTPRatio, InpLot0);
   if(net >= tp_money)
     {
      CloseAll(StringFormat("利確 %.2f", net));
      return;
     }
   if(InpMaxLossMoney > 0.0 && net <= -InpMaxLossMoney)
     {
      CloseAll(StringFormat("安全装置 %.2f", net));
      return;
     }

   //--- 4 本以上は反転で即決済
   if(InpRevFrom >= 1 && c_nlev >= InpRevFrom)
     {
      double back = (bid - c_extreme) * c_dir;
      if(back >= c_step * InpRevRatio)
        {
         CloseAll(StringFormat("反転 %.2f", net));
         return;
        }
     }

   //--- 次のエントリー (ナンピン / 救済)
   if(c_nlev < InpMaxPositions)
     {
      double next = c_ref - c_dir * c_step;
      bool hit = (c_dir > 0) ? (bid <= next) : (bid >= next);
      if(hit && SpreadOK())
        {
         int level = c_nlev + 1;
         int dir = LevelDir(level);
         double lots = NormalizeLots(InpLot0 * MathPow(InpLotMult, level - 1));
         double tp_dist = (dir != c_dir) ? c_step * InpHedgeTPRatio : 0.0;
         if(OpenPosition(dir, lots, tp_dist, level))
           {
            c_nlev = level;
            c_ref = bid;
            SaveState();
            PrintFormat("%d 本目: %s %.2f ロット", level, dir == c_dir ? "ナンピン" : "救済 (逆方向)", lots);
           }
        }
     }
   ShowStatus(bid);
  }

//+------------------------------------------------------------------+
//| 約定があったら確定損益を数え直す (救済ポジションの利確など)       |
//+------------------------------------------------------------------+
void OnTrade()
  {
   g_last_npos = -1;
  }

//+------------------------------------------------------------------+
//| チャート左上に状態を表示                                          |
//+------------------------------------------------------------------+
void ShowStatus(double bid)
  {
   if(MQLInfoInteger(MQL_TESTER) && !MQLInfoInteger(MQL_VISUAL_MODE))
      return;
   if(c_dir == 0)
     {
      Comment("TrendNanpinHedgeEA: 待機中 (初回エントリー待ち)");
      return;
     }
   Comment(StringFormat("TrendNanpinHedgeEA: %s サイクル %d/%d 本\n間隔 %.1f pips  次 %.2f\n損益 %.2f",
                        c_dir > 0 ? "買い" : "売り", c_nlev, InpMaxPositions, c_step / g_pip,
                        c_ref - c_dir * c_step, g_realized + FloatingProfit()));
  }
//+------------------------------------------------------------------+
