cmake_minimum_required(VERSION 3.14)
project(ExecRelayWS C CXX)

# MT4 is 32-bit. When cross-compiling from Linux:
#   cmake -DCMAKE_TOOLCHAIN_FILE=cmake/mingw32.cmake ..
# When building natively on Windows (MSVC or MinGW):
#   cmake -A Win32 ..

add_library(ExecRelayWS SHARED src/ws_dll.cpp)

target_link_libraries(ExecRelayWS PRIVATE ws2_32)

set_target_properties(ExecRelayWS PROPERTIES
    PREFIX  ""
    SUFFIX  ".dll"
    OUTPUT_NAME "ExecRelayWS"
)

if(MSVC)
    target_compile_options(ExecRelayWS PRIVATE /W3 /O2)
else()
    target_compile_options(ExecRelayWS PRIVATE -O2 -Wall -Wextra)
    # Static link runtime so the DLL has no external dependencies.
    target_link_options(ExecRelayWS PRIVATE -static-libgcc -static-libstdc++ -s)
endif()
