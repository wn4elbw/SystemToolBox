cmake_minimum_required(VERSION 3.16)
project(core C)

# ------------------------------------------------------------------
# core — C 最底层：系统信息采集（CPU / 内存 / 磁盘 / 系统 / 开机时长）
# 仅依赖 Win32 API，无任何业务逻辑。
# ------------------------------------------------------------------

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

if(MSVC)
  add_compile_options(/W4 /wd4100)
else()
  add_compile_options(-Wall -Wextra -Wno-unused-parameter)
endif()

add_library(core STATIC
  src/sysinfo.c
)

target_include_directories(core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
)

# psapi 需要显式链接（MinGW 下也自动，但显式更稳）
if(WIN32)
  target_link_libraries(core PRIVATE advapi32 psapi)
endif()

# 供其它目标使用
if(NOT TARGET core)
  add_library(core ALIAS core)
endif()