@echo off
REM Build ExecRelayWS.dll with MSVC (32-bit)
REM Run from a VS 2022 "x86 Native Tools Command Prompt"

set OUT=ExecRelayWS.dll
set SRC=src\ws_dll.cpp

cl /nologo /W3 /O2 /LD /D WIN32 /D _WINDOWS /D _CRT_SECURE_NO_WARNINGS ^
   %SRC% ws2_32.lib ^
   /link /DLL /OUT:%OUT% /MACHINE:X86

if %ERRORLEVEL% == 0 (
    echo Build successful: %OUT%
) else (
    echo Build FAILED
    exit /b 1
)
