/**
 * ExecRelayWS.dll
 * WebSocket client DLL for MT4 EA connectivity to the ExecRelay bridge.
 *
 * Build (MinGW 32-bit, recommended):
 *   i686-w64-mingw32-g++ -O2 -shared -o ExecRelayWS.dll ws_dll.cpp \
 *       -lws2_32 -static-libgcc -static-libstdc++ -s
 *
 * Build (MSVC 32-bit):
 *   cl /W3 /O2 /LD /arch:IA32 ws_dll.cpp ws2_32.lib /link /DLL /OUT:ExecRelayWS.dll
 *
 * MT4 requires a 32-bit DLL placed in <MT4 data folder>/MQL4/Libraries/.
 */

#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0501
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#pragma comment(lib, "ws2_32.lib")

// ─────────────────────────────────────────────────────────────────────────────
// SHA-1 (RFC 3174) — needed for WebSocket handshake accept key verification
// ─────────────────────────────────────────────────────────────────────────────

#define SHA1_BLOCK  64
#define SHA1_DIGEST 20

typedef struct { uint32_t s[5]; uint32_t c[2]; uint8_t buf[SHA1_BLOCK]; } SHA1;

static uint32_t rotl32(uint32_t x, int n) { return (x << n) | (x >> (32 - n)); }

static void sha1_compress(uint32_t s[5], const uint8_t b[SHA1_BLOCK]) {
    uint32_t w[80], a, b2, c, d, e, f, k, t;
    int i;
    for (i = 0; i < 16; i++)
        w[i] = ((uint32_t)b[i*4]<<24)|((uint32_t)b[i*4+1]<<16)|
               ((uint32_t)b[i*4+2]<<8)|(uint32_t)b[i*4+3];
    for (i = 16; i < 80; i++)
        w[i] = rotl32(w[i-3]^w[i-8]^w[i-14]^w[i-16], 1);
    a=s[0]; b2=s[1]; c=s[2]; d=s[3]; e=s[4];
    for (i = 0; i < 80; i++) {
        if      (i<20) { f=(b2&c)|(~b2&d); k=0x5A827999U; }
        else if (i<40) { f=b2^c^d;         k=0x6ED9EBA1U; }
        else if (i<60) { f=(b2&c)|(b2&d)|(c&d); k=0x8F1BBCDCU; }
        else           { f=b2^c^d;         k=0xCA62C1D6U; }
        t=rotl32(a,5)+f+e+k+w[i]; e=d; d=c; c=rotl32(b2,30); b2=a; a=t;
    }
    s[0]+=a; s[1]+=b2; s[2]+=c; s[3]+=d; s[4]+=e;
}

static void sha1_init(SHA1 *ctx) {
    ctx->s[0]=0x67452301U; ctx->s[1]=0xEFCDAB89U;
    ctx->s[2]=0x98BADCFEU; ctx->s[3]=0x10325476U; ctx->s[4]=0xC3D2E1F0U;
    ctx->c[0]=ctx->c[1]=0;
}

static void sha1_update(SHA1 *ctx, const uint8_t *d, size_t len) {
    size_t i, j = (ctx->c[0]>>3)&63;
    if ((ctx->c[0]+=(uint32_t)(len<<3)) < (uint32_t)(len<<3)) ctx->c[1]++;
    ctx->c[1]+=(uint32_t)(len>>29);
    if (j+len > 63) {
        i = 64-j; memcpy(ctx->buf+j, d, i); sha1_compress(ctx->s, ctx->buf);
        for (; i+63 < len; i+=64) sha1_compress(ctx->s, d+i);
        j = 0;
    } else i = 0;
    memcpy(ctx->buf+j, d+i, len-i);
}

static void sha1_final(SHA1 *ctx, uint8_t out[SHA1_DIGEST]) {
    uint8_t fc[8]; uint8_t pad = 0x80; int i;
    for (i=0;i<4;i++) fc[i]  =(uint8_t)(ctx->c[1]>>((3-i)*8));
    for (i=0;i<4;i++) fc[4+i]=(uint8_t)(ctx->c[0]>>((3-i)*8));
    sha1_update(ctx,&pad,1);
    pad=0; while((ctx->c[0]&504)!=448) sha1_update(ctx,&pad,1);
    sha1_update(ctx,fc,8);
    for (i=0;i<SHA1_DIGEST;i++) out[i]=(uint8_t)((ctx->s[i/4]>>((3-(i%4))*8))&0xFF);
}

static void sha1_hash(const uint8_t *d, size_t len, uint8_t out[SHA1_DIGEST]) {
    SHA1 ctx; sha1_init(&ctx); sha1_update(&ctx,d,len); sha1_final(&ctx,out);
}

// ─────────────────────────────────────────────────────────────────────────────
// Base64
// ─────────────────────────────────────────────────────────────────────────────

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static int b64_enc(const uint8_t *in, int len, char *out, int outSize) {
    int i, j=0;
    for (i=0; i<len; i+=3) {
        uint32_t b = (uint32_t)in[i]<<16;
        if (i+1<len) b|=(uint32_t)in[i+1]<<8;
        if (i+2<len) b|=(uint32_t)in[i+2];
        if (j+5>outSize) return -1;
        out[j++]=B64[(b>>18)&0x3F]; out[j++]=B64[(b>>12)&0x3F];
        out[j++]=(i+1<len)?B64[(b>>6)&0x3F]:'=';
        out[j++]=(i+2<len)?B64[b&0x3F]:'=';
    }
    out[j]='\0'; return j;
}

// ─────────────────────────────────────────────────────────────────────────────
// Connection pool
// ─────────────────────────────────────────────────────────────────────────────

#define MAX_CONNS    8
#define IBUF_SIZE    65536
#define PBUF_SIZE    65536

typedef struct {
    SOCKET  sock;
    int     in_use;
    uint8_t ibuf[IBUF_SIZE];   // raw socket receive buffer
    int     ilen;              // bytes stored in ibuf
    CRITICAL_SECTION cs;
} WsConn;

static WsConn    g_conns[MAX_CONNS];
static int       g_wsa_ok = 0;
static CRITICAL_SECTION g_pool_cs;

static void pool_init(void) {
    InitializeCriticalSection(&g_pool_cs);
    memset(g_conns, 0, sizeof(g_conns));
    for (int i=0; i<MAX_CONNS; i++) {
        g_conns[i].sock = INVALID_SOCKET;
        InitializeCriticalSection(&g_conns[i].cs);
    }
}

static int alloc_conn(void) {
    EnterCriticalSection(&g_pool_cs);
    for (int i=0; i<MAX_CONNS; i++) {
        if (!g_conns[i].in_use) {
            g_conns[i].in_use = 1;
            g_conns[i].sock   = INVALID_SOCKET;
            g_conns[i].ilen   = 0;
            LeaveCriticalSection(&g_pool_cs);
            return i;
        }
    }
    LeaveCriticalSection(&g_pool_cs);
    return -1;
}

static void free_conn(int h) {
    if (h < 0 || h >= MAX_CONNS) return;
    EnterCriticalSection(&g_pool_cs);
    WsConn *c = &g_conns[h];
    if (c->sock != INVALID_SOCKET) { closesocket(c->sock); c->sock = INVALID_SOCKET; }
    c->ilen   = 0;
    c->in_use = 0;
    LeaveCriticalSection(&g_pool_cs);
}

static WsConn* get_conn(int h) {
    if (h < 0 || h >= MAX_CONNS || !g_conns[h].in_use) return NULL;
    return &g_conns[h];
}

// ─────────────────────────────────────────────────────────────────────────────
// TCP connect with timeout
// ─────────────────────────────────────────────────────────────────────────────

static SOCKET tcp_connect(const char *host, int port, int timeoutMs) {
    struct addrinfo hints, *res = NULL;
    char portStr[8];
    _snprintf(portStr, sizeof(portStr), "%d", port);
    memset(&hints, 0, sizeof(hints));
    hints.ai_family   = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, portStr, &hints, &res) != 0 || !res) return INVALID_SOCKET;

    SOCKET s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    if (s == INVALID_SOCKET) { freeaddrinfo(res); return INVALID_SOCKET; }

    // Set non-blocking for connect with timeout.
    u_long mode = 1;
    ioctlsocket(s, FIONBIO, &mode);

    connect(s, res->ai_addr, (int)res->ai_addrlen);
    freeaddrinfo(res);

    fd_set wfds, efds;
    FD_ZERO(&wfds); FD_SET(s, &wfds);
    FD_ZERO(&efds); FD_SET(s, &efds);
    struct timeval tv = { timeoutMs/1000, (timeoutMs%1000)*1000 };
    int n = select((int)s+1, NULL, &wfds, &efds, &tv);
    if (n <= 0 || FD_ISSET(s, &efds)) { closesocket(s); return INVALID_SOCKET; }

    // Back to blocking.
    mode = 0;
    ioctlsocket(s, FIONBIO, &mode);
    return s;
}

// ─────────────────────────────────────────────────────────────────────────────
// WebSocket upgrade handshake
// ─────────────────────────────────────────────────────────────────────────────

// Generate a 24-char Base64 websocket key from 16 pseudo-random bytes.
static void ws_make_key(char key[25]) {
    uint8_t raw[16];
    DWORD tick = GetTickCount();
    srand((unsigned)(tick ^ (DWORD)(uintptr_t)key));
    for (int i=0; i<16; i++) raw[i] = (uint8_t)(rand() & 0xFF);
    b64_enc(raw, 16, key, 25);
}

// Compute expected Sec-WebSocket-Accept value.
static void ws_accept(const char *key, char accept[29]) {
    char concat[64];
    _snprintf(concat, sizeof(concat), "%s258EAFA5-E914-47DA-95CA-C5AB0DC85B11", key);
    uint8_t digest[SHA1_DIGEST];
    sha1_hash((const uint8_t*)concat, strlen(concat), digest);
    b64_enc(digest, SHA1_DIGEST, accept, 29);
}

static int ws_handshake(SOCKET s, const char *host, const char *path, int port) {
    char key[25], accept_want[29], buf[2048], line[256];
    ws_make_key(key);
    ws_accept(key, accept_want);

    // Send HTTP upgrade request.
    int len = _snprintf(buf, sizeof(buf),
        "GET %s HTTP/1.1\r\n"
        "Host: %s:%d\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n",
        path, host, port, key);
    if (send(s, buf, len, 0) != len) return 0;

    // Read HTTP response until \r\n\r\n.
    char resp[4096]; int rlen = 0;
    while (rlen < (int)sizeof(resp)-1) {
        int n = recv(s, resp+rlen, 1, 0);
        if (n <= 0) return 0;
        rlen++;
        if (rlen >= 4 &&
            resp[rlen-4]=='\r' && resp[rlen-3]=='\n' &&
            resp[rlen-2]=='\r' && resp[rlen-1]=='\n') break;
    }
    resp[rlen] = '\0';

    // Verify HTTP 101.
    if (!strstr(resp, "101")) return 0;

    // Verify Sec-WebSocket-Accept.
    char *p = strstr(resp, "Sec-WebSocket-Accept:");
    if (!p) return 0;
    p += 21;
    while (*p == ' ') p++;
    char got[64]; int gi=0;
    while (*p && *p!='\r' && *p!='\n' && gi<63) got[gi++]=*p++;
    got[gi]='\0';
    // Trim trailing whitespace.
    while (gi>0 && (got[gi-1]==' '||got[gi-1]=='\t')) got[--gi]='\0';

    return strcmp(got, accept_want) == 0;
}

// ─────────────────────────────────────────────────────────────────────────────
// WebSocket framing
// ─────────────────────────────────────────────────────────────────────────────

// Send a masked text frame (RFC 6455 §5).  Returns 0 on success, -1 on error.
static int ws_send_text(SOCKET s, const char *payload, int payLen) {
    uint8_t frame[10 + 4 + 65536];
    int hlen = 0;

    frame[hlen++] = 0x81;  // FIN=1, opcode=text

    uint8_t mask[4];
    DWORD t = GetTickCount();
    mask[0]=(uint8_t)(t>>24); mask[1]=(uint8_t)(t>>16);
    mask[2]=(uint8_t)(t>>8);  mask[3]=(uint8_t)t;

    if (payLen < 126) {
        frame[hlen++] = (uint8_t)(0x80 | payLen);
    } else if (payLen < 65536) {
        frame[hlen++] = 0xFE;
        frame[hlen++] = (uint8_t)(payLen >> 8);
        frame[hlen++] = (uint8_t)(payLen);
    } else {
        return -1;  // frames > 64KB not needed for our JSON payloads
    }

    frame[hlen++]=mask[0]; frame[hlen++]=mask[1];
    frame[hlen++]=mask[2]; frame[hlen++]=mask[3];

    for (int i=0; i<payLen; i++) frame[hlen+i] = ((uint8_t)payload[i]) ^ mask[i%4];
    hlen += payLen;

    return (send(s, (char*)frame, hlen, 0) == hlen) ? 0 : -1;
}

// Send a pong frame (response to ping, RFC 6455 §5.5.3).
static void ws_send_pong(SOCKET s, const uint8_t *payload, int payLen) {
    uint8_t frame[16 + 4];
    if (payLen > 125) payLen = 125;
    int hlen = 0;
    frame[hlen++] = 0x8A;  // FIN=1, opcode=pong
    frame[hlen++] = (uint8_t)(0x80 | payLen);
    DWORD t = GetTickCount();
    uint8_t mask[4] = {(uint8_t)(t>>24),(uint8_t)(t>>16),(uint8_t)(t>>8),(uint8_t)t};
    frame[hlen++]=mask[0]; frame[hlen++]=mask[1];
    frame[hlen++]=mask[2]; frame[hlen++]=mask[3];
    for (int i=0; i<payLen; i++) frame[hlen+i] = payload[i] ^ mask[i%4];
    send(s, (char*)frame, hlen+payLen, 0);
}

// Parse one complete WebSocket frame from buf[0..bufLen).
// On success: sets *frameLen = total frame bytes consumed, copies payload to
// out[0..*payLen), sets *opcode.
// Returns 1 if a complete frame was parsed, 0 if more data needed, -1 on error.
static int ws_parse_frame(const uint8_t *buf, int bufLen,
                          int *frameLen, uint8_t *opcode,
                          uint8_t *out, int outSize, int *payLen) {
    if (bufLen < 2) return 0;
    *opcode = buf[0] & 0x0F;
    int masked = (buf[1] >> 7) & 1;
    uint64_t plen = buf[1] & 0x7F;
    int hlen = 2;

    if (plen == 126) {
        if (bufLen < 4) return 0;
        plen = ((uint64_t)buf[2]<<8) | buf[3];
        hlen = 4;
    } else if (plen == 127) {
        if (bufLen < 10) return 0;
        plen = 0;
        for (int i=0; i<8; i++) plen = (plen<<8) | buf[2+i];
        hlen = 10;
    }

    if (masked) hlen += 4;
    if (bufLen < hlen + (int)plen) return 0;          // incomplete
    if ((int)plen > outSize)       return -1;          // won't fit

    const uint8_t *src = buf + hlen;
    if (masked) {
        const uint8_t *m = buf + hlen - 4;
        for (uint64_t i=0; i<plen; i++) out[i] = src[i] ^ m[i%4];
    } else {
        memcpy(out, src, (size_t)plen);
    }
    *payLen   = (int)plen;
    *frameLen = hlen + (int)plen;
    return 1;
}

// ─────────────────────────────────────────────────────────────────────────────
// Exported API
// ─────────────────────────────────────────────────────────────────────────────

extern "C" {

/**
 * WsConnect — open a WebSocket connection.
 * Returns a non-negative handle on success, -1 on failure.
 */
__declspec(dllexport) int __stdcall
WsConnect(const char *host, int port, const char *path, int timeoutMs) {
    if (!g_wsa_ok) return -1;

    int h = alloc_conn();
    if (h < 0) return -1;

    WsConn *c = &g_conns[h];
    c->sock = tcp_connect(host, port, timeoutMs > 0 ? timeoutMs : 5000);
    if (c->sock == INVALID_SOCKET) { free_conn(h); return -1; }

    if (!ws_handshake(c->sock, host, path, port)) {
        closesocket(c->sock); c->sock = INVALID_SOCKET;
        free_conn(h); return -1;
    }

    // Set recv timeout to 1 ms for non-blocking-style reads.
    DWORD rv = 1;
    setsockopt(c->sock, SOL_SOCKET, SO_RCVTIMEO, (char*)&rv, sizeof(rv));

    return h;
}

/**
 * WsDisconnect — close the connection and free the handle.
 */
__declspec(dllexport) void __stdcall
WsDisconnect(int handle) {
    WsConn *c = get_conn(handle);
    if (!c) return;
    // Send close frame.
    uint8_t cf[6] = {0x88, 0x82, 0x00, 0x00, 0x00, 0x00};
    if (c->sock != INVALID_SOCKET) send(c->sock, (char*)cf, 6, 0);
    free_conn(handle);
}

/**
 * WsIsConnected — returns 1 if the handle is valid and socket is open.
 */
__declspec(dllexport) int __stdcall
WsIsConnected(int handle) {
    WsConn *c = get_conn(handle);
    return (c && c->sock != INVALID_SOCKET) ? 1 : 0;
}

/**
 * WsSend — send a text frame.
 * data/dataLen are the UTF-8 payload (no null terminator required).
 * Returns 0 on success, -1 on error (connection should be treated as dead).
 */
__declspec(dllexport) int __stdcall
WsSend(int handle, const char *data, int dataLen) {
    WsConn *c = get_conn(handle);
    if (!c || c->sock == INVALID_SOCKET) return -1;
    EnterCriticalSection(&c->cs);
    int r = ws_send_text(c->sock, data, dataLen);
    if (r < 0) { closesocket(c->sock); c->sock = INVALID_SOCKET; }
    LeaveCriticalSection(&c->cs);
    return r;
}

/**
 * WsRead — try to read one complete text frame within timeoutMs.
 *
 * Accumulates incoming socket data in the connection's internal buffer.
 * Control frames (ping/pong/close) are handled transparently:
 *   - Ping → replies with pong, does not return to caller.
 *   - Close → marks connection dead, returns -1.
 *   - Pong → discarded.
 *
 * Returns:
 *   > 0  bytes written to outBuf (one complete text payload)
 *     0  no complete frame available within timeout
 *    -1  connection closed or error
 */
__declspec(dllexport) int __stdcall
WsRead(int handle, char *outBuf, int bufLen, int timeoutMs) {
    WsConn *c = get_conn(handle);
    if (!c || c->sock == INVALID_SOCKET) return -1;

    EnterCriticalSection(&c->cs);

    DWORD deadline = GetTickCount() + (DWORD)timeoutMs;

    for (;;) {
        // Try to parse a complete frame from what we already have buffered.
        while (c->ilen >= 2) {
            uint8_t opcode; int frameLen, payLen;
            uint8_t payload[PBUF_SIZE];

            int r = ws_parse_frame(c->ibuf, c->ilen,
                                   &frameLen, &opcode,
                                   payload, PBUF_SIZE, &payLen);
            if (r < 0) {
                // Framing error; tear down.
                closesocket(c->sock); c->sock = INVALID_SOCKET;
                LeaveCriticalSection(&c->cs);
                return -1;
            }
            if (r == 0) break;  // incomplete frame, read more

            // Consume frame from buffer.
            if (c->ilen > frameLen)
                memmove(c->ibuf, c->ibuf + frameLen, c->ilen - frameLen);
            c->ilen -= frameLen;

            if (opcode == 0x8) {  // close
                closesocket(c->sock); c->sock = INVALID_SOCKET;
                LeaveCriticalSection(&c->cs);
                return -1;
            }
            if (opcode == 0x9) {  // ping → pong
                ws_send_pong(c->sock, payload, payLen);
                continue;
            }
            if (opcode == 0xA) continue;  // pong, discard

            // Text or binary frame: return payload to caller.
            if (payLen > bufLen) payLen = bufLen;
            memcpy(outBuf, payload, payLen);
            LeaveCriticalSection(&c->cs);
            return payLen;
        }

        // Check timeout.
        DWORD now = GetTickCount();
        if (timeoutMs >= 0 && (int)(deadline - now) <= 0) {
            LeaveCriticalSection(&c->cs);
            return 0;
        }

        // Try to read more data from the socket.
        int avail = IBUF_SIZE - c->ilen;
        if (avail <= 0) {
            // Buffer full without a complete frame — bad state.
            closesocket(c->sock); c->sock = INVALID_SOCKET;
            LeaveCriticalSection(&c->cs);
            return -1;
        }
        int n = recv(c->sock, (char*)(c->ibuf + c->ilen), avail, 0);
        if (n > 0) {
            c->ilen += n;
            continue;
        }
        if (n == 0 || WSAGetLastError() == WSAECONNRESET) {
            closesocket(c->sock); c->sock = INVALID_SOCKET;
            LeaveCriticalSection(&c->cs);
            return -1;
        }
        // WSAETIMEDOUT / WSAEWOULDBLOCK → no data yet.
        if (timeoutMs == 0) {
            LeaveCriticalSection(&c->cs);
            return 0;
        }
        // Small spin-wait before retrying.
        LeaveCriticalSection(&c->cs);
        Sleep(2);
        EnterCriticalSection(&c->cs);
        if (c->sock == INVALID_SOCKET) {
            LeaveCriticalSection(&c->cs);
            return -1;
        }
    }
}

} // extern "C"

// ─────────────────────────────────────────────────────────────────────────────
// DLL entry point
// ─────────────────────────────────────────────────────────────────────────────

BOOL APIENTRY DllMain(HMODULE /*hMod*/, DWORD reason, LPVOID /*res*/) {
    switch (reason) {
    case DLL_PROCESS_ATTACH: {
        WSADATA wd;
        if (WSAStartup(MAKEWORD(2,2), &wd) == 0) g_wsa_ok = 1;
        pool_init();
        break;
    }
    case DLL_PROCESS_DETACH:
        for (int i=0; i<MAX_CONNS; i++) {
            if (g_conns[i].in_use) free_conn(i);
        }
        if (g_wsa_ok) WSACleanup();
        break;
    }
    return TRUE;
}
