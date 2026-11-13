"""실제 C main·controller·planner health의 지연 반례. RTOS와 장치 전송은 fixture다."""
from pathlib import Path
import subprocess

import pytest
from test_command_attempt_main import HARNESS, HOME, PLATFORM, compile_main


@pytest.mark.parametrize('pattern,first,moving',[
    ('at_us>=20000&&at_us<=100000',6,55),
    ('at_us==20000||at_us==60000||at_us==100000||at_us==140000',0,56),
])
def test_actual_main_drops_single_deadlines_and_stops_on_fifth(tmp_path,pattern,first,moving):
    harness=HARNESS.replace(
        'if(at_us<600000||at_us>=1000000)out->sample.reason="STATE_PREPARING";\n'
        ' else if(at_us>=800000)out->sample.v_mps=.3f;',
        f'if({pattern})out->sample.reason="DEADLINE";else out->sample.v_mps=.3f;')
    start=harness.index(' assert(loops==60&&reads[0]==60')
    end=harness.index(' printf(',start)
    harness=harness[:start]+f'''
 assert(loops==60&&reads[0]==60);
 assert(first_restricted_iteration=={first});
 assert(restricted_attempts=={int(bool(first))}&&moving_attempts=={moving});
'''+harness[end:]
    result=compile_main(tmp_path,harness,['CONFIG_XENOVA_DRIVE_WARM=1','CONFIG_XENOVA_DRIVE_LATTICE=1'])
    assert result['restricted_attempts']==int(bool(first))


def native(tmp_path,name,source,extra=()):
    mqtt=PLATFORM/'apps/common/xenova-mqtt-c'
    includes=(HOME/'zephyr/src',PLATFORM/'apps/common/xenova-drive-runtime/include',
        PLATFORM/'guests/zephyr/diagnostics/include',mqtt/'include',mqtt/'third_party/jsmn',
        PLATFORM/'apps/autonomous/zephyr/src',PLATFORM/'apps/manual/zephyr/src')
    binary=tmp_path/name
    result=subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
        '-fsanitize=address,undefined',*['-I'+str(p) for p in includes],str(source),
        *map(str,extra),'-lm','-o',str(binary)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    result=subprocess.run([str(binary)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    return result.stdout


def test_controller_retry_identity_and_previous_completion_lifetime(tmp_path):
    mqtt=PLATFORM/'apps/common/xenova-mqtt-c'
    extra=[HOME/'zephyr/src/controller.c',HOME/'zephyr/src/adapter.c',
        PLATFORM/'guests/zephyr/diagnostics/src/health.c',
        PLATFORM/'apps/autonomous/zephyr/src/autonomous.c',PLATFORM/'apps/manual/zephyr/src/manual.c']
    extra.extend(mqtt/'src'/name for name in ('xm_topic.c','xm_policy.c','xm_envelope.c',
        'xm_json.c','xm_seq.c','xm_payload.c','xm_manual.c'))
    assert 'transient deadlines:' in native(tmp_path,'controller',HOME/'tests/controller.c',extra)


def test_planner_operation_deadline_and_owner_observation_are_separate(tmp_path):
    assert 'planner-health:' in native(tmp_path,'planner-health',HOME/'tests/planner_health.c')
