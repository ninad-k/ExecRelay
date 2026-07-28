//+------------------------------------------------------------------+
//| ExecRelay.mq4                                                    |
//| Author  : Ninad K                                                |
//| Core    : Connects to ExecRelay Bridge via ExecRelayWS.dll       |
//|           Registers with instance_id on connect, auto-reconnects |
//|           Executes buy/sell/pending/close commands from bridge   |
//|           Reports fills and errors back over the same socket     |
//+------------------------------------------------------------------+
#property copyright "Ninad K"
#property version   "1.00"
#property strict

// ExecRelayWS.dll must be in <MT4 data folder>/MQL4/Libraries/
#import "ExecRelayWS.dll"
   int  WsConnect   (const uchar &host[], int port, const uchar &path[], int timeoutMs);
   void WsDisconnect(int handle);
   int  WsIsConnected(int handle);
   int  WsSend      (int handle, const uchar &data[], int dataLen);
   int  WsRead      (int handle, uchar &outBuf[], int bufLen, int timeoutMs);
#import

input string InpBridgeHost    = "127.0.0.1";  // Bridge host
input int    InpBridgePort    = 8082;          // Bridge WebSocket port
input string InpInstanceID    = "";            // Instance ID (from ExecRelay dashboard)
input string InpBridgeToken   = "";            // Bridge auth token (BRIDGE_AUTH_TOKEN)
input int    InpMagicNumber   = 20240101;      // Magic number for orders placed by this EA
input string InpEAVersion     = "1.00";        // Reported EA version
input int    InpTimerMs       = 200;           // Poll interval ms
input int    InpHeartbeatMs   = 30000;         // Heartbeat interval ms (0 = disabled)
// Signals carry the sender's symbol verbatim; map it to this broker's naming
// here. Pairs first ("GOLD=XAUUSD;US30=US30.Cash"), then suffix for everything
// unmapped ("EURUSD" + ".r" -> "EURUSD.r").
input string InpSymbolMap     = "";            // Symbol map "FROM=TO;FROM=TO"
input string InpSymbolSuffix  = "";            // Suffix appended to unmapped symbols
// When a pending order is rejected for being on the wrong side of the market
// (err 129/130), execute at market instead; the order comment gets an "_M"
// suffix so fallback entries are distinguishable in the account history.
input bool   InpPendingFallbackMarket = true;  // Market fallback for wrong-side pendings

int  g_handle        = -1;
bool g_registered    = false;
uint g_lastConn      = 0;
uint g_lastHeartbeat = 0;
uint g_startTick     = 0;

// Pending orders placed by this EA that have not activated yet, so their
// real fill (or cancellation) can be reported when it happens. In-memory:
// orders placed before an EA restart lose activation reporting (documented).
int    g_pendTickets[];
string g_pendTraces[];

//+------------------------------------------------------------------+
int OnInit()
{
    if(InpInstanceID == "")
    {
        Alert("ExecRelay: InpInstanceID must be set");
        return INIT_PARAMETERS_INCORRECT;
    }
    g_startTick = GetTickCount();
    EventSetMillisecondTimer(InpTimerMs);
    return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
    EventKillTimer();
    WsClose();
}

void OnTimer()
{
    if(!WsAlive())
    {
        uint now = GetTickCount();
        if(now - g_lastConn >= 3000)
            WsConnect();
        return;
    }
    uchar buf[];
    ArrayResize(buf, 4096);
    int n;
    while((n = WsRead(g_handle, buf, 4096, 5)) > 0)
    {
        string msg = CharArrayToString(buf, 0, n, CP_UTF8);
        HandleMessage(msg);
    }
    if(n < 0) { WsClose(); return; }

    if(g_registered && InpHeartbeatMs > 0)
    {
        uint now = GetTickCount();
        if(now - g_lastHeartbeat >= (uint)InpHeartbeatMs)
        {
            SendHeartbeat();
            g_lastHeartbeat = now;
        }
    }

    // Only while registered: a fill sent into a dead/unregistered socket
    // would be dropped and the activation would never be reported.
    if(g_registered)
        CheckPendingActivations();
}

void OnTick() {}

//+------------------------------------------------------------------+
// WebSocket layer (via DLL)
//+------------------------------------------------------------------+

bool WsAlive()
{
    return g_handle >= 0 && WsIsConnected(g_handle) == 1;
}

void WsClose()
{
    if(g_handle >= 0)
    {
        WsDisconnect(g_handle);
        g_handle     = -1;
        g_registered = false;
    }
}

void WsConnect()
{
    g_lastConn = GetTickCount();
    WsClose();

    uchar hostBuf[], pathBuf[];
    StringToCharArray(InpBridgeHost, hostBuf, 0, WHOLE_ARRAY, CP_ACP);
    StringToCharArray("/ea/ws",      pathBuf, 0, WHOLE_ARRAY, CP_ACP);

    g_handle = WsConnect(hostBuf, InpBridgePort, pathBuf, 5000);
    if(g_handle < 0)
    {
        Print("ExecRelay: connect failed host=", InpBridgeHost, " port=", InpBridgePort);
        return;
    }
    SendRegister();
}

bool WsSendText(const string text)
{
    if(g_handle < 0) return false;
    uchar buf[];
    int len = StringToCharArray(text, buf, 0, WHOLE_ARRAY, CP_UTF8) - 1;
    if(len <= 0) return true;
    return WsSend(g_handle, buf, len) == 0;
}

//+------------------------------------------------------------------+
// JSON helpers
//+------------------------------------------------------------------+

string JStr(const string key, const string val)
{
    return "\"" + key + "\":\"" + val + "\"";
}

string JGetStr(const string json, const string key)
{
    string needle = "\"" + key + "\":\"";
    int pos = StringFind(json, needle);
    if(pos < 0) return "";
    pos += StringLen(needle);
    int end = StringFind(json, "\"", pos);
    return end < 0 ? "" : StringSubstr(json, pos, end - pos);
}

string JGetObj(const string json, const string key)
{
    string needle = "\"" + key + "\":{";
    int pos = StringFind(json, needle);
    if(pos < 0) return "";
    pos += StringLen(needle) - 1;
    int depth = 0;
    for(int i = pos; i < StringLen(json); i++)
    {
        ushort c = StringGetCharacter(json, i);
        if(c == '{') depth++;
        else if(c == '}') { if(--depth == 0) return StringSubstr(json, pos, i - pos + 1); }
    }
    return "";
}

double PDouble(const string obj, const string key, double def = 0.0)
{
    string v = JGetStr(obj, key);
    return v == "" ? def : StringToDouble(v);
}

//+------------------------------------------------------------------+
// Protocol
//+------------------------------------------------------------------+

void HandleMessage(const string msg)
{
    string mtype = JGetStr(msg, "type");

    if(mtype == "registered")
    {
        g_registered = true;
        Print("ExecRelay: registered instance_id=", InpInstanceID);
        return;
    }
    if(!g_registered) return;

    if(mtype == "signal")
    {
        ExecuteSignal(JGetStr(msg, "trace_id"),
                      JGetStr(msg, "command"),
                      JGetStr(msg, "symbol"),
                      JGetObj(msg, "params"));
        return;
    }
    if(mtype == "ping") WsSendText("{\"type\":\"pong\"}");
}

void SendRegister()
{
    string msg = "{"
        + JStr("type",           "register")                        + ","
        + JStr("instance_id",    InpInstanceID)                     + ","
        + JStr("token",          InpBridgeToken)                    + ","
        + JStr("account_number", IntegerToString(AccountNumber()))  + ","
        + JStr("broker",         AccountCompany())                  + ","
        + JStr("platform",       "mt4")                             + ","
        + JStr("ea_version",     InpEAVersion)
        + "}";
    WsSendText(msg);
}

void SendHeartbeat()
{
    double freeMargin = AccountFreeMargin();
    double equity     = AccountEquity();
    uint   uptime     = (GetTickCount() - g_startTick) / 1000;
    string msg = "{"
        + JStr("type", "heartbeat") + ","
        + "\"free_margin\":" + DoubleToString(freeMargin, 2) + ","
        + "\"equity\":"      + DoubleToString(equity,     2) + ","
        + "\"uptime_secs\":" + IntegerToString(uptime)
        + "}";
    WsSendText(msg);
}

void SendFill(const string traceID, const string status,
              const string orderID, const string errCode, const string errMsg)
{
    string msg = "{"
        + JStr("type",            "fill")   + ","
        + JStr("trace_id",        traceID)  + ","
        + JStr("status",          status)   + ","
        + JStr("broker_order_id", orderID)  + ","
        + JStr("error_code",      errCode)  + ","
        + JStr("error_message",   errMsg)
        + "}";
    WsSendText(msg);
}

//+------------------------------------------------------------------+
// Trade execution
//+------------------------------------------------------------------+

double PipSize(const string sym)
{
    int    d = (int)MarketInfo(sym, MODE_DIGITS);
    double p = MarketInfo(sym, MODE_POINT);
    return (d == 5 || d == 3) ? p * 10.0 : p;
}

// Translate the signal's symbol to this broker's naming: exact pair from
// InpSymbolMap wins; otherwise InpSymbolSuffix is appended (unless already
// present). Mapping lives EA-side because broker naming is per-terminal.
string MapSymbol(const string sym)
{
    if(InpSymbolMap != "")
    {
        string pairs[];
        int n = StringSplit(InpSymbolMap, ';', pairs);
        for(int i = 0; i < n; i++)
        {
            string p = pairs[i];
            StringTrimLeft(p); StringTrimRight(p);
            int eq = StringFind(p, "=");
            if(eq <= 0) continue;
            if(StringSubstr(p, 0, eq) == sym)
                return StringSubstr(p, eq + 1);
        }
    }
    if(InpSymbolSuffix != "")
    {
        int sfxLen = StringLen(InpSymbolSuffix);
        int symLen = StringLen(sym);
        if(symLen < sfxLen || StringSubstr(sym, symLen - sfxLen) != InpSymbolSuffix)
            return sym + InpSymbolSuffix;
    }
    return sym;
}

void TrackPending(const int ticket, const string traceID)
{
    int n = ArraySize(g_pendTickets);
    ArrayResize(g_pendTickets, n + 1);
    ArrayResize(g_pendTraces,  n + 1);
    g_pendTickets[n] = ticket;
    g_pendTraces[n]  = traceID;
}

void UntrackPending(const int idx)
{
    int last = ArraySize(g_pendTickets) - 1;
    g_pendTickets[idx] = g_pendTickets[last];
    g_pendTraces[idx]  = g_pendTraces[last];
    ArrayResize(g_pendTickets, last);
    ArrayResize(g_pendTraces,  last);
}

// A pending order was reported as "placed" when created; this reports the
// real outcome: "filled" once it activates into a market position, or
// "cancelled" if it was deleted/expired before activating.
void CheckPendingActivations()
{
    for(int i = ArraySize(g_pendTickets) - 1; i >= 0; i--)
    {
        if(!OrderSelect(g_pendTickets[i], SELECT_BY_TICKET))
        {
            // Ticket no longer resolvable; stop tracking.
            UntrackPending(i);
            continue;
        }
        int otype = OrderType();
        if(otype == OP_BUY || otype == OP_SELL)
        {
            SendFill(g_pendTraces[i], "filled", IntegerToString(g_pendTickets[i]), "", "");
            UntrackPending(i);
        }
        else if(OrderCloseTime() > 0)
        {
            SendFill(g_pendTraces[i], "cancelled", IntegerToString(g_pendTickets[i]),
                     "", "pending order deleted or expired before activation");
            UntrackPending(i);
        }
    }
}

void ExecuteSignal(const string traceID, const string cmd,
                   const string rawSym,  const string params)
{
    string sym = MapSymbol(rawSym);
    if(!SymbolSelect(sym, true))
    {
        SendFill(traceID, "rejected", "", "SYMBOL_UNKNOWN",
                 "symbol not found on this broker: " + sym +
                 " (from " + rawSym + "; check InpSymbolMap/InpSymbolSuffix)");
        return;
    }

    double vol    = PDouble(params, "vol_lots");
    double sl     = PDouble(params, "sl");
    double tp     = PDouble(params, "tp");
    double slPips = PDouble(params, "sl_pips");
    double tpPips = PDouble(params, "tp_pips");
    // Flat-path signals canonicalize the pending entry as "entry_price";
    // the JSON /webhook/ml path historically used "entry". Accept both.
    double entry  = PDouble(params, "entry");
    if(entry == 0.0) entry = PDouble(params, "entry_price");

    // The signal's strategy tag becomes the broker order comment (truncated
    // to leave room for the "_M" fallback suffix within MT4's limit).
    string cmt = JGetStr(params, "comment");
    if(cmt == "") cmt = "ExecRelay";
    if(StringLen(cmt) > 27) cmt = StringSubstr(cmt, 0, 27);

    double ask = MarketInfo(sym, MODE_ASK);
    double bid = MarketInfo(sym, MODE_BID);
    double pip = PipSize(sym);

    bool isBuy = (cmd == "buy" || cmd == "buystop" || cmd == "buylimit");
    if(sl == 0.0 && slPips > 0.0) sl = isBuy ? ask - slPips * pip : bid + slPips * pip;
    if(tp == 0.0 && tpPips > 0.0) tp = isBuy ? ask + tpPips * pip : bid - tpPips * pip;

    if(cmd == "buy" || cmd == "sell")
    {
        if(vol <= 0.0) { SendFill(traceID, "rejected", "", "VOL_MISSING", "vol_lots required"); return; }
        int optype  = (cmd == "buy") ? OP_BUY : OP_SELL;
        double price = (cmd == "buy") ? ask : bid;
        int ticket = OrderSend(sym, optype, vol, price, 3, sl, tp, cmt, InpMagicNumber, 0, clrNONE);
        if(ticket > 0) SendFill(traceID, "filled",   IntegerToString(ticket), "", "");
        else           SendFill(traceID, "error", "", IntegerToString(GetLastError()),
                                "OrderSend failed err=" + IntegerToString(GetLastError()));
        return;
    }

    int pendingType = -1;
    if(cmd == "buystop")   pendingType = OP_BUYSTOP;
    if(cmd == "sellstop")  pendingType = OP_SELLSTOP;
    if(cmd == "buylimit")  pendingType = OP_BUYLIMIT;
    if(cmd == "selllimit") pendingType = OP_SELLLIMIT;
    if(pendingType >= 0)
    {
        if(vol <= 0.0 || entry <= 0.0)
        {
            SendFill(traceID, "rejected", "", "PARAM_MISSING", "vol_lots and entry required");
            return;
        }
        int ticket = OrderSend(sym, pendingType, vol, entry, 3, sl, tp, cmt, InpMagicNumber, 0, clrNONE);
        if(ticket > 0)
        {
            // "placed" = pending order accepted by the broker, NOT executed.
            // The real fill is reported by CheckPendingActivations().
            SendFill(traceID, "placed", IntegerToString(ticket), "", "");
            TrackPending(ticket, traceID);
            return;
        }
        int pendErr = GetLastError();
        if(InpPendingFallbackMarket && (pendErr == 129 || pendErr == 130))
        {
            // Wrong side of the market: execute at market instead, tag _M.
            RefreshRates();
            double mprice = isBuy ? MarketInfo(sym, MODE_ASK) : MarketInfo(sym, MODE_BID);
            int mticket = OrderSend(sym, isBuy ? OP_BUY : OP_SELL, vol, mprice, 3,
                                    sl, tp, cmt + "_M", InpMagicNumber, 0, clrNONE);
            if(mticket > 0)
            {
                SendFill(traceID, "filled", IntegerToString(mticket), "",
                         "pending wrong-side (err " + IntegerToString(pendErr) +
                         "); executed at market (_M)");
                return;
            }
            SendFill(traceID, "error", "", IntegerToString(GetLastError()),
                     "pending err " + IntegerToString(pendErr) +
                     "; market fallback failed err=" + IntegerToString(GetLastError()));
            return;
        }
        SendFill(traceID, "error", "", IntegerToString(pendErr),
                 "OrderSend pending failed err=" + IntegerToString(pendErr));
        return;
    }

    if(cmd == "closebuy" || cmd == "closesell" || cmd == "closeall")
    {
        ClosePositions(traceID, sym, cmd); return;
    }
    if(cmd == "cancel") { CancelPending(traceID, sym); return; }

    SendFill(traceID, "rejected", "", "UNKNOWN_CMD", "unhandled command: " + cmd);
}

void ClosePositions(const string traceID, const string sym, const string cmd)
{
    int closed = 0, errors = 0;
    for(int i = OrdersTotal() - 1; i >= 0; i--)
    {
        if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
        if(OrderSymbol()      != sym)              continue;
        if(OrderMagicNumber() != InpMagicNumber)   continue;

        int otype = OrderType();
        if(otype != OP_BUY && otype != OP_SELL) continue;  // skip pending
        if(cmd == "closebuy"  && otype != OP_BUY)  continue;
        if(cmd == "closesell" && otype != OP_SELL) continue;

        double closePrice = (otype == OP_BUY)
            ? MarketInfo(sym, MODE_BID)
            : MarketInfo(sym, MODE_ASK);

        if(OrderClose(OrderTicket(), OrderLots(), closePrice, 3, clrNONE)) closed++;
        else errors++;
    }
    SendFill(traceID, errors == 0 ? "filled" : "error", "", "",
             "closed=" + IntegerToString(closed) + " errors=" + IntegerToString(errors));
}

void CancelPending(const string traceID, const string sym)
{
    int cancelled = 0, errors = 0;
    for(int i = OrdersTotal() - 1; i >= 0; i--)
    {
        if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
        if(OrderSymbol()      != sym)            continue;
        if(OrderMagicNumber() != InpMagicNumber) continue;
        if(OrderType() < OP_BUYLIMIT)            continue;  // skip market orders

        if(OrderDelete(OrderTicket(), clrNONE)) cancelled++;
        else errors++;
    }
    SendFill(traceID, errors == 0 ? "filled" : "error", "", "",
             "cancelled=" + IntegerToString(cancelled));
}
