# ExecRelayWS.dll

WebSocket client DLL for MT4 EA connectivity to the ExecRelay bridge.

MT4 has no native TCP socket API (unlike MT5 build 2715+). This DLL provides five
exported functions that the MT4 EA uses to maintain a persistent WebSocket connection.

## Exported API

```c
// Open a WebSocket connection. Returns a handle (≥0) or -1 on failure.
int  __stdcall WsConnect(const char* host, int port, const char* path, int timeoutMs);

// Close the connection and free the handle.
void __stdcall WsDisconnect(int handle);

// Returns 1 if connected, 0 if not.
int  __stdcall WsIsConnected(int handle);

// Send a text frame. data/dataLen are the UTF-8 payload (no null terminator needed).
// Returns 0 on success, -1 on error.
int  __stdcall WsSend(int handle, const char* data, int dataLen);

// Read one complete text frame within timeoutMs milliseconds.
// Control frames (ping/pong/close) are handled transparently:
//   ping  → pong is sent automatically
//   close → returns -1
// Returns: bytes written to outBuf, 0 if no frame arrived, -1 on disconnect/error.
int  __stdcall WsRead(int handle, char* outBuf, int bufLen, int timeoutMs);
```

## Build

### Option A — MinGW cross-compile from Linux

```sh
apt-get install gcc-mingw-w64-i686 g++-mingw-w64-i686
mkdir build && cd build
cmake -DCMAKE_TOOLCHAIN_FILE=../cmake/mingw32.cmake ..
make
# Output: build/ExecRelayWS.dll
```

### Option B — MSVC on Windows

Open a **Visual Studio x86 Native Tools Command Prompt**, then:

```bat
build.bat
# Output: ExecRelayWS.dll
```

### Option C — MinGW on Windows

```sh
i686-w64-mingw32-g++ -O2 -shared -o ExecRelayWS.dll src/ws_dll.cpp \
    -lws2_32 -static-libgcc -static-libstdc++ -s
```

## Installation

Copy `ExecRelayWS.dll` to:

```
<MT4 data folder>/MQL4/Libraries/ExecRelayWS.dll
```

The MT4 data folder is usually `C:\Users\<user>\AppData\Roaming\MetaQuotes\Terminal\<id>\`.
You can also open it from MT4 via **File → Open Data Folder**.

## Notes

- The DLL must be **32-bit**. MT4 is a 32-bit application on all platforms.
- Only one copy of the DLL runs per MT4 terminal instance. Up to 8 concurrent
  WebSocket connections are supported (`MAX_CONNS` in `ws_dll.cpp`).
- SHA-1 is implemented inline (no external crypto dependency). The only system
  dependency is `ws2_32.dll` (WinSock2), which ships with all Windows versions.
- Strings are passed as `uchar[]` (ANSI/UTF-8). See `ea/mt4/ExecRelay.mq4` for
  usage examples.
