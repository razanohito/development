//+------------------------------------------------------------------+
//|                                                    ExportM1.mq5  |
//|  チャートの銘柄の1分足を CSV に書き出すスクリプト。                |
//|  出力: MQL5/Files/<銘柄>_1m.csv (datetime,open,high,low,close,volume)|
//|  Python バックテスターに長期データを渡すために使う。              |
//+------------------------------------------------------------------+
#property script_show_inputs

input datetime InpFrom        = D'2020.01.01';  // 開始日 (サーバー時刻)
input int      InpServerGMT   = 3;              // サーバー時刻の GMT オフセット (時間)。UTC に変換して出力

void OnStart()
  {
   MqlRates rates[];
   ArraySetAsSeries(rates, false);
   int n = CopyRates(_Symbol, PERIOD_M1, InpFrom, TimeCurrent(), rates);
   if(n <= 0)
     {
      Print("ExportM1: 1分足を取得できませんでした err=", GetLastError(),
            " (ツール→オプション→チャート の最大バー数を増やし、履歴をダウンロードしてください)");
      return;
     }
   string name = _Symbol + "_1m.csv";
   int h = FileOpen(name, FILE_WRITE | FILE_ANSI | FILE_TXT);
   if(h == INVALID_HANDLE)
     {
      Print("ExportM1: ファイルを開けません err=", GetLastError());
      return;
     }
   FileWriteString(h, "datetime,open,high,low,close,volume\n");
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   for(int i = 0; i < n; i++)
     {
      datetime t = rates[i].time - InpServerGMT * 3600;
      string ts = TimeToString(t, TIME_DATE | TIME_MINUTES);
      StringReplace(ts, ".", "-");
      StringReplace(ts, " ", "T");
      FileWriteString(h, StringFormat("%s:00+00:00,%s,%s,%s,%s,%I64d\n", ts,
                                      DoubleToString(rates[i].open, digits),
                                      DoubleToString(rates[i].high, digits),
                                      DoubleToString(rates[i].low, digits),
                                      DoubleToString(rates[i].close, digits),
                                      rates[i].tick_volume));
     }
   FileClose(h);
   PrintFormat("ExportM1: %d 本を %s に書き出しました", n, name);
  }
