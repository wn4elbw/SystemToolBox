# SystemToolBox — C/C++ 原生层顶层工程
#
# 用法：
#   cmake -S . -B build -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release
#   cmake --build build
#   产物：build/bin/native_core.dll
#
# 或直接执行 scripts/build.ps1（自动探测 MSVC / MinGW / runtime 工具链）。

cmake_minimum_required(VERSION 3.16)
project(SystemToolBoxNative C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)

add_subdirectory(core)
add_subdirectory(native)

# 便捷目标：把 DLL 拷到 python 后端预期的位置
add_custom_command(TARGET native_core POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
          $<TARGET_FILE:native_core>
          ${CMAKE_SOURCE_DIR}/python/native/native_core.dll
  COMMENT "Copy native_core.dll -> python/native/"
)