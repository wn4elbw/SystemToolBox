cmake_minimum_required(VERSION 3.16)
project(native_core CXX)

# ------------------------------------------------------------------
# native_core.dll — C++ 兼容层 / 底层交互 / 驱动管理 / 权限控制
# 导出纯 C ABI，供 Python ctypes 加载。
# ------------------------------------------------------------------

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 支持独立构建（native/ 单独 configure），也支持被顶层工程引用
if(NOT TARGET core)
  add_subdirectory(${CMAKE_CURRENT_SOURCE_DIR}/../core
                   ${CMAKE_CURRENT_BINARY_DIR}/core)
endif()

add_library(native_core SHARED
  src/native.cpp
  src/service.cpp
  src/elevate.cpp
)

target_compile_definitions(native_core PRIVATE
  NATIVE_CORE_EXPORTS
  UNICODE
  _UNICODE
)

target_include_directories(native_core PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/include
)

target_link_libraries(native_core PRIVATE
  core
  advapi32
  shell32
  psapi
)

if(MSVC)
  target_compile_options(native_core PRIVATE /W4 /wd4251)
else()
  target_compile_options(native_core PRIVATE -Wall -Wextra -Wno-unused-parameter)
endif()

set_target_properties(native_core PROPERTIES
  OUTPUT_NAME "native_core"
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
  LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
  ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib"
)