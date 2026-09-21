//+------------------------------------------------------------------+
//| ExportDealsCSV.mq5                                                |
//| Writes the account's deal history to a CSV for the trading        |
//| journal's MetaTrader 5 importer (Import page -> MetaTrader 5).    |
//|                                                                   |
//| Install: copy to <terminal data folder>\MQL5\Scripts, compile in  |
//| MetaEditor (F7), then drag "ExportDealsCSV" onto any chart.       |
//| Output:  <terminal data folder>\MQL5\Files\TDJournal_deals.csv    |
//|          (or Common\Files when InpCommonFolder is true).          |
//|                                                                   |
//| One row per buy/sell deal. Balance, deposit, credit and similar   |
//| deals are not written. Times are raw broker server time; the      |
//| importer converts them.                                           |
//+------------------------------------------------------------------+
#property copyright "TDJournal"
#property version   "1.00"
#property description "Exports deal history to CSV for the trading journal."
#property script_show_inputs

input datetime InpFrom         = D'2000.01.01 00:00';   // Export deals from
input datetime InpTo           = 0;                     // Export deals to (0 = now)
input string   InpSymbols      = "";                    // Only these symbols, comma separated (empty = all)
input string   InpFileName     = "TDJournal_deals.csv"; // Output file name
input bool     InpCommonFolder = false;                 // Write to the shared Common\Files folder

//--- per-symbol details, looked up once per symbol
string g_name[];
string g_path[];
int    g_digits[];
double g_point[];
double g_contract[];

//+------------------------------------------------------------------+
//| Keep every value on one CSV cell: no commas, quotes, line breaks |
//| or non-ASCII characters.                                         |
//+------------------------------------------------------------------+
string Clean(const string s)
{
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(s, i);
      if(c == ',')
         c = ';';
      else if(c == '"')
         c = '\'';
      else if(c < 32 || c > 126)
         c = ' ';
      out += ShortToString(c);
   }
   return out;
}

//+------------------------------------------------------------------+
//| Is this symbol wanted by the InpSymbols filter?                  |
//+------------------------------------------------------------------+
bool WantSymbol(const string symbol, const string &filter[], const int filterCount)
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

//+------------------------------------------------------------------+
//| Load path, digits, point and contract size for a symbol.         |
//| A symbol that is not in Market Watch is selected briefly so its  |
//| properties can be read, then put back.                           |
//+------------------------------------------------------------------+
int SpecIndex(const string symbol)
{
   int count = ArraySize(g_name);
   for(int i = 0; i < count; i++)
      if(g_name[i] == symbol)
         return i;

   ArrayResize(g_name, count + 1);
   ArrayResize(g_path, count + 1);
   ArrayResize(g_digits, count + 1);
   ArrayResize(g_point, count + 1);
   ArrayResize(g_contract, count + 1);
   g_name[count]     = symbol;
   g_path[count]     = "";
   g_digits[count]   = -1;
   g_point[count]    = 0.0;
   g_contract[count] = 0.0;

   if(SymbolInfoInteger(symbol, SYMBOL_EXIST) == 0)
      return count;

   bool wasSelected = (SymbolInfoInteger(symbol, SYMBOL_SELECT) != 0);
   bool selectedNow = false;
   if(!wasSelected)
      selectedNow = SymbolSelect(symbol, true);

   g_path[count]     = SymbolInfoString(symbol, SYMBOL_PATH);
   g_digits[count]   = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   g_point[count]    = SymbolInfoDouble(symbol, SYMBOL_POINT);
   g_contract[count] = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);

   if(selectedNow)
      SymbolSelect(symbol, false);
   return count;
}

string EntryText(const long entry)
{
   switch((int)entry)
   {
      case DEAL_ENTRY_IN:     return "in";
      case DEAL_ENTRY_OUT:    return "out";
      case DEAL_ENTRY_INOUT:  return "inout";
      case DEAL_ENTRY_OUT_BY: return "out_by";
   }
   return "unknown";
}

//+------------------------------------------------------------------+
void OnStart()
{
   datetime toTime = (InpTo == 0) ? TimeCurrent() : InpTo;

   if(!HistorySelect(InpFrom, toTime))
   {
      Alert("ExportDealsCSV: could not load the deal history (error ", GetLastError(), ").");
      return;
   }

   long marginMode = AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(marginMode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
      Alert("ExportDealsCSV: this account is not a hedging account. ",
            "The journal importer supports hedging accounts; netting reversals will be rejected.");

   //--- optional symbol filter
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

   int flags = FILE_WRITE | FILE_CSV | FILE_ANSI;
   if(InpCommonFolder)
      flags |= FILE_COMMON;
   int handle = FileOpen(InpFileName, flags, ',');
   if(handle == INVALID_HANDLE)
   {
      Alert("ExportDealsCSV: could not open ", InpFileName, " for writing (error ", GetLastError(), ").");
      return;
   }

   string currency = Clean(AccountInfoString(ACCOUNT_CURRENCY));
   string server   = Clean(AccountInfoString(ACCOUNT_SERVER));

   FileWrite(handle,
             "deal_ticket", "position_id", "time", "symbol", "type", "entry",
             "volume", "price", "commission", "fee", "swap", "profit",
             "magic", "comment", "account_currency", "server",
             "symbol_path", "digits", "point", "contract_size");

   int total   = HistoryDealsTotal();
   int written = 0;

   for(int i = 0; i < total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;

      long dealType = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(dealType != DEAL_TYPE_BUY && dealType != DEAL_TYPE_SELL)
         continue;                                   // balance, credit, charge, ...

      string symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
      if(!WantSymbol(symbol, filter, filterCount))
         continue;

      int    s      = SpecIndex(symbol);
      int    digits = (g_digits[s] >= 0) ? g_digits[s] : 8;

      datetime when = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);

      FileWrite(handle,
                (string)ticket,
                (string)HistoryDealGetInteger(ticket, DEAL_POSITION_ID),
                TimeToString(when, TIME_DATE | TIME_SECONDS),
                Clean(symbol),
                (dealType == DEAL_TYPE_BUY) ? "buy" : "sell",
                EntryText(HistoryDealGetInteger(ticket, DEAL_ENTRY)),
                DoubleToString(HistoryDealGetDouble(ticket, DEAL_VOLUME), 4),
                DoubleToString(HistoryDealGetDouble(ticket, DEAL_PRICE), digits),
                DoubleToString(HistoryDealGetDouble(ticket, DEAL_COMMISSION), 2),
                DoubleToString(HistoryDealGetDouble(ticket, DEAL_FEE), 2),
                DoubleToString(HistoryDealGetDouble(ticket, DEAL_SWAP), 2),
                DoubleToString(HistoryDealGetDouble(ticket, DEAL_PROFIT), 2),
                (string)HistoryDealGetInteger(ticket, DEAL_MAGIC),
                Clean(HistoryDealGetString(ticket, DEAL_COMMENT)),
                currency,
                server,
                Clean(g_path[s]),
                (g_digits[s] >= 0) ? (string)g_digits[s] : "",
                (g_point[s] > 0.0) ? DoubleToString(g_point[s], 10) : "",
                (g_contract[s] > 0.0) ? DoubleToString(g_contract[s], 2) : "");
      written++;
   }

   FileClose(handle);

   string folder = InpCommonFolder
                   ? TerminalInfoString(TERMINAL_COMMONDATA_PATH) + "\\Files\\"
                   : TerminalInfoString(TERMINAL_DATA_PATH) + "\\MQL5\\Files\\";
   Alert("ExportDealsCSV: wrote ", written, " deals to ", folder, InpFileName);
}
//+------------------------------------------------------------------+
