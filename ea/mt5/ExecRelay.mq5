//+------------------------------------------------------------------+
//| ExecRelay.mq5                                                    |
//| Author  : Ninad K                                                |
//| Core    : Connects to ExecRelay Bridge via persistent WebSocket  |
//|           Registers with instance_id on connect, auto-reconnects |
//|           Executes buy/sell/pending/close commands from bridge   |
//|           Reports fills and errors back over the same socket     |
//+------------------------------------------------------------------+
#property copyright "Ninad K"
#property version   "1.00"

#include <Trade\Trade.mqh>

input string InpBridgeHost    = "127.0.0.1";  // Bridge host
input int    InpBridgePort    = 8082;          // Bridge WebSocket port
input string InpInstanceID    = "";            // Instance ID (from ExecRelay dashboard)
input string InpBridgeToken   = "";            // Bridge auth token (BRIDGE_AUTH_TOKEN)
input int    InpMagicNumber   = 20240101;      // Magic number for orders placed by this EA
input string InpEAVersion     = "1.00";        // Reported EA version
input int    InpTimerMs       = 200;           // Poll interval ms
input int    InpHeartbeatMs   = 30000;         // Heartbeat interval ms (0 = disabled)

int    g_socket        = INVALID_HANDLE;
bool   g_registered    = false;
uint   g_lastConn      = 0;
uint   g_lastHeartbeat = 0;
uint   g_startTick     = 0;
CTrade g_trade;

//+------------------------------------------------------------------+
int OnInit()
{
    if(InpInstanceID == "")
    {
        Alert("ExecRelay: InpInstanceID must be set");
        return INIT_PARAMETERS_INCORRECT;
    }
    g_trade.SetExpertMagicNumber(InpMagicNumber);
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
    if(!WsIsAlive())
    {
        uint now = GetTickCount();
        if(now - g_lastConn >= 3000)
            WsConnect();
        return;
    }
    string msg;
    while(WsReadText(msg))
        HandleMessage(msg);

    if(g_registered && InpHeartbeatMs > 0)
    {
        uint now = GetTickCount();
        if(now - g_lastHeartbeat >= (uint)InpHeartbeatMs)
        {
            SendHeartbeat();
            g_lastHeartbeat = now;
        }
    }
}

void OnTick() {}

//+------------------------------------------------------------------+
// WebSocket layer
//+------------------------------------------------------------------+

bool WsIsAlive()
{
    return g_socket != INVALID_HANDLE && SocketIsConnected(g_socket);
}

void WsClose()
{
    if(g_socket != INVALID_HANDLE)
    {
        SocketClose(g_socket);
        g_socket     = INVALID_HANDLE;
        g_registered = false;
    }
}

void WsConnect()
{
    g_lastConn = GetTickCount();
    WsClose();

    g_socket = SocketCreate();
    if(g_socket == INVALID_HANDLE)
    {
        Print("ExecRelay: SocketCreate failed");
        return;
    }
    if(!SocketConnect(g_socket, InpBridgeHost, InpBridgePort, 3000))
    {
        Print("ExecRelay: connect failed host=", InpBridgeHost, " port=", InpBridgePort);
        WsClose();
        return;
    }
    if(!WsHandshake())
    {
        Print("ExecRelay: WebSocket handshake failed");
        WsClose();
        return;
    }
    SendRegister();
}

bool WsHandshake()
{
    string req = "GET /ea/ws HTTP/1.1\r\n"
                 "Host: "                  + InpBridgeHost + ":" + IntegerToString(InpBridgePort) + "\r\n"
                 "Upgrade: websocket\r\n"
                 "Connection: Upgrade\r\n"
                 "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                 "Sec-WebSocket-Version: 13\r\n"
                 "\r\n";
    uchar buf[];
    int   len = StringToCharArray(req, buf, 0, StringLen(req));
    if(SocketSend(g_socket, buf, len) < len) return false;

    string resp = "";
    uchar  b[1];
    for(int i = 0; i < 4096; i++)
    {
        if(SocketRead(g_socket, b, 1, 3000) != 1) return false;
        resp += CharArrayToString(b, 0, 1);
        int rlen = StringLen(resp);
        if(rlen >= 4 && StringSubstr(resp, rlen - 4) == "\r\n\r\n") break;
    }
    return StringFind(resp, " 101 ") >= 0;
}

void MaskKey(uchar &key[])
{
    ArrayResize(key, 4);
    ulong ts = GetMicrosecondCount();
    key[0] = (uchar)( ts        & 0xFF);
    key[1] = (uchar)((ts >> 8)  & 0xFF);
    key[2] = (uchar)((ts >> 16) & 0xFF);
    key[3] = (uchar)((ts >> 24) & 0xFF);
}

bool WsSendText(const string text)
{
    int plen = StringLen(text);
    if(plen == 0) return true;

    uchar payload[];
    ArrayResize(payload, plen);
    StringToCharArray(text, payload, 0, plen);

    uchar key[];
    MaskKey(key);
    for(int i = 0; i < plen; i++) payload[i] ^= key[i % 4];

    uchar frame[];
    int   hlen;
    if(plen <= 125)
    {
        ArrayResize(frame, 2 + 4 + plen);
        frame[0] = 0x81;
        frame[1] = (uchar)(0x80 | plen);
        frame[2] = key[0]; frame[3] = key[1];
        frame[4] = key[2]; frame[5] = key[3];
        hlen = 6;
    }
    else
    {
        ArrayResize(frame, 4 + 4 + plen);
        frame[0] = 0x81;
        frame[1] = (uchar)(0x80 | 126);
        frame[2] = (uchar)(plen >> 8);
        frame[3] = (uchar)(plen & 0xFF);
        frame[4] = key[0]; frame[5] = key[1];
        frame[6] = key[2]; frame[7] = key[3];
        hlen = 8;
    }
    for(int i = 0; i < plen; i++) frame[hlen + i] = payload[i];
    int total = ArraySize(frame);
    return SocketSend(g_socket, frame, total) == total;
}

// Try to read exactly count bytes from the socket. Returns false on timeout or error.
// Closes connection if partially-read frame cannot be completed (partial frame = broken stream).
bool SockReadN(uchar &buf[], int count, int timeout_ms, bool closeOnFail)
{
    int got = 0;
    while(got < count)
    {
        uchar tmp[];
        ArrayResize(tmp, count - got);
        int n = SocketRead(g_socket, tmp, count - got, timeout_ms);
        if(n <= 0)
        {
            if(closeOnFail) WsClose();
            return false;
        }
        for(int i = 0; i < n; i++) buf[got + i] = tmp[i];
        got += n;
    }
    return true;
}

bool WsReadText(string &text)
{
    if(!SocketIsConnected(g_socket)) return false;

    // Non-blocking peek: first header byte with very short timeout
    uchar b0[1];
    if(SocketRead(g_socket, b0, 1, 5) != 1) return false;

    // Frame started — read second header byte with longer timeout
    uchar b1[1];
    if(SocketRead(g_socket, b1, 1, 1000) != 1) { WsClose(); return false; }

    int  opcode = b0[0] & 0x0F;
    bool masked = (b1[0] & 0x80) != 0;
    int  plen   = b1[0] & 0x7F;

    if(plen == 126)
    {
        uchar ext[]; ArrayResize(ext, 2);
        if(!SockReadN(ext, 2, 1000, true)) return false;
        plen = (ext[0] << 8) | ext[1];
    }
    else if(plen == 127)
    {
        uchar ext[]; ArrayResize(ext, 8);
        if(!SockReadN(ext, 8, 1000, true)) return false;
        plen = (int)((((long)ext[4]) << 24) | (ext[5] << 16) | (ext[6] << 8) | ext[7]);
    }

    uchar maskKey[];
    if(masked)
    {
        ArrayResize(maskKey, 4);
        if(!SockReadN(maskKey, 4, 1000, true)) return false;
    }

    uchar payload[];
    ArrayResize(payload, plen > 0 ? plen : 1);
    if(plen > 0 && !SockReadN(payload, plen, 3000, true)) return false;

    if(masked)
        for(int i = 0; i < plen; i++) payload[i] ^= maskKey[i % 4];

    if(opcode == 0x8) { WsClose(); return false; }
    if(opcode == 0x9)                              // ping → pong
    {
        uchar key[]; MaskKey(key);
        uchar pong[];
        ArrayResize(pong, 2 + 4 + plen);
        pong[0] = 0x8A;
        pong[1] = (uchar)(0x80 | plen);
        pong[2] = key[0]; pong[3] = key[1];
        pong[4] = key[2]; pong[5] = key[3];
        for(int i = 0; i < plen; i++) pong[6 + i] = payload[i] ^ key[i % 4];
        SocketSend(g_socket, pong, ArraySize(pong));
        return false;
    }
    if(opcode != 0x1 && opcode != 0x2) return false;

    text = CharArrayToString(payload, 0, plen);
    return true;
}

//+------------------------------------------------------------------+
// JSON helpers — only what the protocol requires
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
    if(mtype == "ping")
        WsSendText("{\"type\":\"pong\"}");
}

void SendRegister()
{
    string msg = "{"
        + JStr("type",           "register")       + ","
        + JStr("instance_id",    InpInstanceID)    + ","
        + JStr("token",          InpBridgeToken)   + ","
        + JStr("account_number", IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN))) + ","
        + JStr("broker",         AccountInfoString(ACCOUNT_COMPANY)) + ","
        + JStr("platform",       "mt5")             + ","
        + JStr("ea_version",     InpEAVersion)
        + "}";
    WsSendText(msg);
}

void SendHeartbeat()
{
    double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
    double equity     = AccountInfoDouble(ACCOUNT_EQUITY);
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
        + JStr("type",            "fill")    + ","
        + JStr("trace_id",        traceID)   + ","
        + JStr("status",          status)    + ","
        + JStr("broker_order_id", orderID)   + ","
        + JStr("error_code",      errCode)   + ","
        + JStr("error_message",   errMsg)
        + "}";
    WsSendText(msg);
}

//+------------------------------------------------------------------+
// Trade execution
//+------------------------------------------------------------------+

double PipSize(const string sym)
{
    int    d = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
    double p = SymbolInfoDouble(sym, SYMBOL_POINT);
    return (d == 3 || d == 5) ? 10.0 * p : p;
}

void ExecuteSignal(const string traceID, const string cmd,
                   const string sym,     const string params)
{
    double vol    = PDouble(params, "vol_lots");
    double sl     = PDouble(params, "sl");
    double tp     = PDouble(params, "tp");
    double slPips = PDouble(params, "sl_pips");
    double tpPips = PDouble(params, "tp_pips");
    double entry  = PDouble(params, "entry");

    double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
    double bid = SymbolInfoDouble(sym, SYMBOL_BID);
    double pip = PipSize(sym);

    bool isBuy = (cmd == "buy" || cmd == "buystop" || cmd == "buylimit");
    if(sl == 0.0 && slPips > 0.0) sl = isBuy ? ask - slPips * pip : bid + slPips * pip;
    if(tp == 0.0 && tpPips > 0.0) tp = isBuy ? ask + tpPips * pip : bid - tpPips * pip;

    if(cmd == "buy" || cmd == "sell")
    {
        if(vol <= 0.0) { SendFill(traceID, "rejected", "", "VOL_MISSING", "vol_lots required"); return; }
        bool ok = (cmd == "buy") ? g_trade.Buy(vol, sym, 0, sl, tp)
                                 : g_trade.Sell(vol, sym, 0, sl, tp);
        if(ok) SendFill(traceID, "filled", IntegerToString(g_trade.ResultOrder()), "", "");
        else   SendFill(traceID, "error",  "", IntegerToString(g_trade.ResultRetcode()),
                        g_trade.ResultRetcodeDescription());
        return;
    }

    ENUM_ORDER_TYPE otype = -1;
    if(cmd == "buystop")   otype = ORDER_TYPE_BUY_STOP;
    if(cmd == "sellstop")  otype = ORDER_TYPE_SELL_STOP;
    if(cmd == "buylimit")  otype = ORDER_TYPE_BUY_LIMIT;
    if(cmd == "selllimit") otype = ORDER_TYPE_SELL_LIMIT;
    if(otype != -1)
    {
        if(vol <= 0.0 || entry <= 0.0) { SendFill(traceID, "rejected", "", "PARAM_MISSING", "vol_lots and entry required"); return; }
        if(g_trade.OrderOpen(sym, otype, vol, 0, entry, sl, tp))
            SendFill(traceID, "filled", IntegerToString(g_trade.ResultOrder()), "", "");
        else
            SendFill(traceID, "error", "", IntegerToString(g_trade.ResultRetcode()),
                     g_trade.ResultRetcodeDescription());
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
    for(int i = PositionsTotal() - 1; i >= 0; i--)
    {
        ulong ticket = PositionGetTicket(i);
        if(ticket == 0) continue;
        if(PositionGetString(POSITION_SYMBOL) != sym) continue;
        if(PositionGetInteger(POSITION_MAGIC)  != InpMagicNumber) continue;

        ENUM_POSITION_TYPE pt = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
        if(cmd == "closebuy"  && pt != POSITION_TYPE_BUY)  continue;
        if(cmd == "closesell" && pt != POSITION_TYPE_SELL) continue;

        if(g_trade.PositionClose(ticket)) closed++;
        else errors++;
    }
    string status = (errors == 0) ? "filled" : "error";
    SendFill(traceID, status, "", "",
             "closed=" + IntegerToString(closed) + " errors=" + IntegerToString(errors));
}

void CancelPending(const string traceID, const string sym)
{
    int cancelled = 0, errors = 0;
    for(int i = OrdersTotal() - 1; i >= 0; i--)
    {
        ulong ticket = OrderGetTicket(i);
        if(ticket == 0) continue;
        if(OrderGetString(ORDER_SYMBOL) != sym) continue;
        if(OrderGetInteger(ORDER_MAGIC)  != InpMagicNumber) continue;

        if(g_trade.OrderDelete(ticket)) cancelled++;
        else errors++;
    }
    string status = (errors == 0) ? "filled" : "error";
    SendFill(traceID, status, "", "", "cancelled=" + IntegerToString(cancelled));
}
