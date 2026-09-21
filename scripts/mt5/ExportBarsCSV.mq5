//+------------------------------------------------------------------+
//| ExportBarsCSV.mq5                                                 |
//| Writes M1 price bars around every trade in the account's deal     |
//| history, for the trading journal's FX charts and MFE/MAE.         |
//|                                                                   |
//| Install: copy to <terminal data folder>\MQL5\Scripts, compile in  |
//| MetaEditor (F7), then drag "ExportBarsCSV" onto any chart.        |
//|                                                                   |
//| Before the first run: Tools > Options > Charts > "Max bars in     |
//| chart" = Unlimited, then restart MT5. Otherwise MT5 will not hand |
//| out M1 bars older than a few weeks.                               |
//|                                                                   |
//| Output: one file per symbol, TDJournal_bars_<SYMBOL>_M1.csv, in   |
//| the shared Common\Files folder (the path is shown when it ends;   |
//| put it in backend/.env as MT5_BARS_DIR). Only the hours around    |
//| your trades are written, so the files stay small. Times are raw   |
//| broker server time; the journal converts them.                    |
//+------------------------------------------------------------------+
#property copyright "TDJournal"
#property version   "1.00"
#property description "Exports M1 bars around your trades for the trading journal."
#property script_show_inputs

input datetime InpFrom          = D'2000.01.01 00:00';   // Cover trades from
input datetime InpTo            = 0;                     // Cover trades to (0 = now)
input string   InpSymbols       = "";                    // Only these symbols, comma separated (empty = all)
input int      InpPadBeforeHrs  = 24;                    // Hours of bars before each trade
input int      InpPadAfterHrs   = 24;                    // Hours of bars after each trade
input string   InpFilePrefix    = "TDJournal_bars_";     // Output file name prefix
input bool     InpCommonFolder  = true;                  // Write to the shared Common\Files folder

string   g_sym[];    // one entry per deal: its symbol
datetime g_tm[];     // and its time

//+------------------------------------------------------------------+
bool Wanted(const string symbol, const string &filter[], const int filterCount)
{
   if(filterCount == 0)
      return true;
   string up = symbol;
   StringToUpper(up);
   for(int i = 0; i < filterCount; i++)
      if(filter[i] == up)
         return true;
   return false;
}

//--- file-name safe version of a symbol
string SafeName(const string s)
{
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(s, i);
      bool ok = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                c == '.' || c == '_' || c == '#' || c == '-';
      if(!ok)
         c = '_';
      out += ShortToString(c);
   }
   return out;
}

//--- distinct symbols, in first-seen order
int UniqueSymbols(string &list[])
{
   ArrayResize(list, 0);
   int n = ArraySize(g_sym);
   for(int i = 0; i < n; i++)
   {
      bool seen = false;
      for(int j = 0; j < ArraySize(list); j++)
         if(list[j] == g_sym[i]) { seen = true; break; }
      if(!seen)
      {
         int k = ArraySize(list);
         ArrayResize(list, k + 1);
         list[k] = g_sym[i];
      }
   }
   return ArraySize(list);
}

//--- read M1 bars for [from, to]; a symbol that is not in Market Watch or not yet downloaded
//--- may need a moment, so try a few times
int FetchBars(const string symbol, const datetime from, const datetime to, MqlRates &rates[])
{
   for(int attempt = 0; attempt < 5; attempt++)
   {
      ResetLastError();
      int got = CopyRates(symbol, PERIOD_M1, from, to, rates);
      if(got > 0)
         return got;
      Sleep(1000);
   }
   return 0;
}

//+------------------------------------------------------------------+
void OnStart()
{
   datetime toTime = (InpTo == 0) ? TimeCurrent() : InpTo;
   if(!HistorySelect(InpFrom, toTime))
   {
      Alert("ExportBarsCSV: could not load the deal history (error ", GetLastError(), ").");
      return;
   }

   string filter[];
   int filterCount = 0;
   if(StringLen(InpSymbols) > 0)
   {
      filterCount = StringSplit(InpSymbols, ',', filter);
      for(int i = 0; i < filterCount; i++)
      {
         StringTrimLeft(filter[i]);
         StringTrimRight(filter[i]);
         StringToUpper(filter[i]);
      }
   }

   //--- every buy/sell deal's symbol and time
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      long dealType = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(dealType != DEAL_TYPE_BUY && dealType != DEAL_TYPE_SELL)
         continue;
      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      if(symbol == "" || !Wanted(symbol, filter, filterCount))
         continue;
      int k = ArraySize(g_sym);
      ArrayResize(g_sym, k + 1);
      ArrayResize(g_tm, k + 1);
      g_sym[k] = symbol;
      g_tm[k]  = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
   }

   if(ArraySize(g_sym) == 0)
   {
      Alert("ExportBarsCSV: no deals found in that range.");
      return;
   }

   string symbols[];
   int symbolCount = UniqueSymbols(symbols);

   int flags = FILE_WRITE | FILE_CSV | FILE_ANSI;
   if(InpCommonFolder)
      flags |= FILE_COMMON;

   long padBefore = (long)InpPadBeforeHrs * 3600;
   long padAfter  = (long)InpPadAfterHrs * 3600;
   string report  = "";
   int filesWritten = 0;

   for(int s = 0; s < symbolCount; s++)
   {
      string symbol = symbols[s];

      //--- this symbol's deal times, ascending
      datetime times[];
      ArrayResize(times, 0);
      for(int i = 0; i < ArraySize(g_sym); i++)
         if(g_sym[i] == symbol)
         {
            int k = ArraySize(times);
            ArrayResize(times, k + 1);
            times[k] = g_tm[i];
         }
      ArraySort(times);

      //--- merge the windows around each deal
      datetime winStart[];
      datetime winEnd[];
      ArrayResize(winStart, 0);
      ArrayResize(winEnd, 0);
      datetime now = TimeCurrent();
      for(int i = 0; i < ArraySize(times); i++)
      {
         datetime a = (datetime)((long)times[i] - padBefore);
         datetime b = (datetime)((long)times[i] + padAfter);
         if(b > now)
            b = now;
         int w = ArraySize(winStart);
         if(w > 0 && a <= winEnd[w - 1])
         {
            if(b > winEnd[w - 1])
               winEnd[w - 1] = b;
         }
         else
         {
            ArrayResize(winStart, w + 1);
            ArrayResize(winEnd, w + 1);
            winStart[w] = a;
            winEnd[w]   = b;
         }
      }

      if(!SymbolSelect(symbol, true))
         Print("ExportBarsCSV: could not add ", symbol, " to Market Watch (continuing).");
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      if(digits <= 0)
         digits = 5;

      string fileName = InpFilePrefix + SafeName(symbol) + "_M1.csv";
      int handle = FileOpen(fileName, flags, ',');
      if(handle == INVALID_HANDLE)
      {
         Alert("ExportBarsCSV: could not open ", fileName, " for writing (error ", GetLastError(), ").");
         continue;
      }
      FileWrite(handle, "time", "open", "high", "low", "close", "tick_volume");

      int barsWritten = 0;
      int windowsWithoutData = 0;
      for(int w = 0; w < ArraySize(winStart); w++)
      {
         MqlRates rates[];
         int got = FetchBars(symbol, winStart[w], winEnd[w], rates);
         if(got <= 0)
         {
            windowsWithoutData++;
            continue;
         }
         for(int r = 0; r < got; r++)
         {
            FileWrite(handle,
                      TimeToString(rates[r].time, TIME_DATE | TIME_SECONDS),
                      DoubleToString(rates[r].open, digits),
                      DoubleToString(rates[r].high, digits),
                      DoubleToString(rates[r].low, digits),
                      DoubleToString(rates[r].close, digits),
                      (string)rates[r].tick_volume);
            barsWritten++;
         }
      }
      FileClose(handle);
      filesWritten++;

      report += symbol + ": " + (string)barsWritten + " bars in " + (string)ArraySize(winStart) + " window(s)";
      if(windowsWithoutData > 0)
         report += " (" + (string)windowsWithoutData + " window(s) had no data)";
      report += "\n";
   }

   string folder = InpCommonFolder
                   ? TerminalInfoString(TERMINAL_COMMONDATA_PATH) + "\\Files"
                   : TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files";
   Print("ExportBarsCSV finished.\n", report, "Folder: ", folder);
   Alert("ExportBarsCSV: wrote ", filesWritten, " file(s).\nSet MT5_BARS_DIR=", folder, "\n", report);
}
//+------------------------------------------------------------------+
