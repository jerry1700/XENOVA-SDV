# ESP32 feedback-3 통신 처리 호스트 시험 (ESP32 없이 gcc)
#   cmake -S vehicle/ESP32/test/host -B build-host && cmake --build build-host && ctest --test-dir build-host --output-on-failure
cmake_minimum_required(VERSION 3.20)
project(xenova_esp32_host_tests C)

option(FB3_SANITIZE "ASan·UBSan 으로 시험한다" ON)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(FIRMWARE_SOURCE ${CMAKE_CURRENT_SOURCE_DIR}/../../xenova_esp32/src)
set(FB3_SOURCE ${FIRMWARE_SOURCE}/feedback3)
set(TOOLS ${CMAKE_CURRENT_SOURCE_DIR}/../../tools)
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
add_custom_command(
  OUTPUT ${CMAKE_CURRENT_BINARY_DIR}/link_vectors.inc
  COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/gen_vectors.py --wires ${PINNED_VECTORS} ${CMAKE_CURRENT_BINARY_DIR}/link_vectors.inc
  DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/gen_vectors.py ${PINNED_VECTORS}
  VERBATIM)

# 호스트 실행 파일: C99, 경고는 오류, 켜져 있으면 ASan·UBSan
function(fb3_host_executable name)
  add_executable(${name} ${ARGN})
  set_target_properties(${name} PROPERTIES C_STANDARD 99 C_STANDARD_REQUIRED ON)
  target_include_directories(${name} PRIVATE ${FIRMWARE_SOURCE} ${CMAKE_CURRENT_BINARY_DIR})
  target_compile_options(${name} PRIVATE -Wall -Wextra -Werror)
  if(FB3_SANITIZE)
    target_compile_options(${name} PRIVATE -fsanitize=address,undefined)
    target_link_options(${name} PRIVATE -fsanitize=address,undefined)
  endif()
endfunction()

set(FB3_CODEC ${FB3_SOURCE}/wire.c ${FB3_SOURCE}/payloads.c)
set(FB3_LINK ${FB3_CODEC} ${FB3_SOURCE}/cutoff.c ${FB3_SOURCE}/link.c)

enable_testing()
fb3_host_executable(test_feedback3 test_feedback3.c ${FB3_CODEC} ${CMAKE_CURRENT_BINARY_DIR}/vectors.inc)
add_test(NAME esp32_feedback3 COMMAND test_feedback3)

fb3_host_executable(test_link test_link.c ${FB3_LINK} ${CMAKE_CURRENT_BINARY_DIR}/link_vectors.inc)
add_test(NAME esp32_link COMMAND test_link)

fb3_host_executable(test_quadrature test_quadrature.c)
add_test(NAME esp32_quadrature COMMAND test_quadrature)

# PC 가 Pi 역할을 하는 점검 도구를 호스트로 빌드한 통신 처리에 돌린다. 실보드에서 쓰는 것과 같은 도구다
fb3_host_executable(sim_link sim_link.c ${FB3_LINK})
if(WIN32)
  target_link_libraries(sim_link PRIVATE winmm)
endif()
add_test(NAME esp32_link_bench COMMAND ${Python3_EXECUTABLE} ${TOOLS}/link_bench.py --sim $<TARGET_FILE:sim_link>)
add_test(NAME esp32_steer_check COMMAND ${Python3_EXECUTABLE} ${TOOLS}/steer_check.py --sim $<TARGET_FILE:sim_link> --hold 0.1)

# 점검 도구가 답이 끊기거나 검증한 것이 없을 때 통과로 끝나지 않는지 가짜 장치로 본다
add_test(NAME esp32_tools COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/test_tools.py)

# unpack 이 초기화되지 않은 출력 구조체의 값을 읽지 않는지 GCC 정적 분석으로 본다. 컴파일만 한다
if(CMAKE_C_COMPILER_ID STREQUAL "GNU" AND CMAKE_C_COMPILER_VERSION VERSION_GREATER_EQUAL 12)
  add_library(analyze_unpack OBJECT analyze_unpack.c)
  set_target_properties(analyze_unpack PROPERTIES C_STANDARD 99 C_STANDARD_REQUIRED ON)
  target_include_directories(analyze_unpack PRIVATE ${FIRMWARE_SOURCE})
  target_compile_options(analyze_unpack PRIVATE -Wall -Wextra -Werror -fanalyzer)
endif()
