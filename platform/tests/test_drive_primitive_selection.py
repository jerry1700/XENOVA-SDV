"""실제 앱 CMake의 primitive 선택만 검증한다. Zephyr/ARM 빌드는 별도다."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'platform/apps/drive/zephyr'


class DrivePrimitiveSelectionTest(unittest.TestCase):
    def check_selection(self, development, expected):
        cmake = os.environ.get('CMAKE_EXECUTABLE') or shutil.which('cmake')
        if not cmake:
            self.skipTest('existing CMake executable required; no dependency installation')
        with tempfile.TemporaryDirectory(prefix='drive-primitive-selection-') as temporary:
            script = Path(temporary) / 'selection.cmake'
            # 빌드 시스템의 외부 Zephyr 초기화만 대체한다. 조건과 target_sources는
            # 복제하지 않고 실제 앱 CMake 전체를 실행해 최종 source를 관찰한다.
            script.write_text('''cmake_minimum_required(VERSION 3.20.5)
function(find_package package)
  if(NOT package STREQUAL "Zephyr")
    message(FATAL_ERROR "unexpected external package")
  endif()
endfunction()
function(project)
endfunction()
function(target_include_directories)
endfunction()
function(target_sources_ifdef)
endfunction()
function(target_sources target visibility)
  if(NOT target STREQUAL "app")
    message(FATAL_ERROR "unexpected source owner")
  endif()
  set_property(GLOBAL APPEND PROPERTY observed_app_sources ${ARGN})
endfunction()
set(CONFIG_XENOVA_DRIVE_LATTICE ON)
set(CONFIG_NET_NATIVE OFF)
set(CMAKE_CURRENT_SOURCE_DIR "${APP_SOURCE_DIR}")
include("${APP_CMAKE_FILE}")
get_property(sources GLOBAL PROPERTY observed_app_sources)
list(FILTER sources INCLUDE REGEX "(^|/)primitives[.]c$")
list(LENGTH sources count)
if(NOT count EQUAL 1)
  message(FATAL_ERROR "expected one primitive source; found ${count}: ${sources}")
endif()
list(GET sources 0 actual)
if(NOT EXISTS "${actual}")
  message(FATAL_ERROR "selected primitive source is missing: ${actual}")
endif()
get_filename_component(actual "${actual}" REALPATH)
get_filename_component(expected "${EXPECTED_SOURCE}" REALPATH)
if(NOT actual STREQUAL expected)
  message(FATAL_ERROR "primitive selection differs: ${actual} != ${expected}")
endif()
message(STATUS "selected primitive: ${actual}")
''', encoding='utf-8')
            # 이 override는 원 commit의 CMake에 대한 음성 대조용 시험 입력이다.
            app_file = Path(os.environ.get('DRIVE_TEST_CMAKE_FILE', APP / 'CMakeLists.txt'))
            command = [cmake, '-DAPP_SOURCE_DIR='+APP.as_posix(),
                '-DAPP_CMAKE_FILE='+app_file.as_posix(), '-DEXPECTED_SOURCE='+expected.as_posix()]
            if development is not None:
                command.append('-DXENOVA_DRIVE_DEVELOPMENT_PROFILE='+development)
            result = subprocess.run([*command, '-P', str(script)], capture_output=True,
                text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_unset_profile_selects_original_lab_source(self):
        self.check_selection(None, ROOT / 'platform/apps/drive/planner/generated/primitives.c')

    def test_explicit_off_selects_original_lab_source(self):
        self.check_selection('OFF', ROOT / 'platform/apps/drive/planner/generated/primitives.c')

    def test_explicit_on_selects_existing_development_source(self):
        self.check_selection('ON', ROOT / 'platform/apps/drive/profiles/course-development-1/primitives.c')


if __name__ == '__main__':
    unittest.main()
