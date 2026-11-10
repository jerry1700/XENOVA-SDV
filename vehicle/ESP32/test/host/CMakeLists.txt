# ESP32 feedback-3 통신 처리 호스트 시험 (ESP32 없이 gcc)
#   cmake -S vehicle/ESP32/test/host -B build-host && cmake --build build-host && ctest --test-dir build-host --output-on-failure
cmake_minimum_required(VERSION 3.20)
project(xenova_esp32_host_tests C)

option(FB3_SANITIZE "ASan·UBSan 으로 시험한다" ON)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(FB3_SOURCE ${CMAKE_CURRENT_SOURCE_DIR}/../../xenova_esp32/src/feedback3)
set(PINNED_VECTORS ${CMAKE_CURRENT_SOURCE_DIR}/gateway-feedback-3.json)
set(CONTRACT_VECTORS ${CMAKE_CURRENT_SOURCE_DIR}/../../../../docs/contracts/vectors/gateway-feedback-3.json)

# 계약 vector 의 복사본으로 시험한다. 저장소에 원본이 있으면 두 파일이 같아야 한다
if(EXISTS ${CONTRACT_VECTORS})
  file(SHA256 ${CONTRACT_VECTORS} contract_sha256)
  file(SHA256 ${PINNED_VECTORS} pinned_sha256)
  if(NOT contract_sha256 STREQUAL pinned_sha256)
    message(FATAL_ERROR "gateway-feedback-3.json 복사본이 docs/contracts/vectors 의 원본과 다르다. 원본을 다시 복사한다")
  endif()
endif()

add_custom_command(
  OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/vectors.inc
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/gen_vectors.py ${PINNED_VECTORS} ${CMAKE_CURRENT_BINARY_DIR}/vectors.inc
  DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/gen_vectors.py ${PINNED_VECTORS}
  VERBATIM)

enable_testing()
add_executable(test_feedback3 test_feedback3.c ${FB3_SOURCE}/wire.c ${FB3_SOURCE}/payloads.c ${CMAKE_CURRENT_BINARY_DIR}/vectors.inc)
set_target_properties(test_feedback3 PROPERTIES C_STANDARD 99 C_STANDARD_REQUIRED ON)
target_include_directories(test_feedback3 PRIVATE ${FB3_SOURCE} ${CMAKE_CURRENT_BINARY_DIR})
target_compile_options(test_feedback3 PRIVATE -Wall -Wextra -Werror)
if(FB3_SANITIZE)
  target_compile_options(test_feedback3 PRIVATE -fsanitize=address,undefined)
  target_link_options(test_feedback3 PRIVATE -fsanitize=address,undefined)
endif()
add_test(NAME esp32_feedback3 COMMAND test_feedback3)

# unpack 이 초기화되지 않은 출력 구조체의 값을 읽지 않는지 GCC 정적 분석으로 본다. 컴파일만 한다
if(CMAKE_C_COMPILER_ID STREQUAL "GNU" AND CMAKE_C_COMPILER_VERSION VERSION_GREATER_EQUAL 12)
  add_library(analyze_unpack OBJECT analyze_unpack.c)
  set_target_properties(analyze_unpack PROPERTIES C_STANDARD 99 C_STANDARD_REQUIRED ON)
  target_include_directories(analyze_unpack PRIVATE ${FB3_SOURCE})
  target_compile_options(analyze_unpack PRIVATE -Wall -Wextra -Werror -fanalyzer)
endif()
