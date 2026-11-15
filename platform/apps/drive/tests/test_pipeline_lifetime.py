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
    harness=harness.replace('static const char *last_reason;', '''
static const char *last_reason;
static uint64_t last_deadline_report;
static unsigned distinct_deadline_reports;
''').replace('supervisions++;return 0;', '''
 if(strstr(payload,"\\"phase\\":\\"EXECUTING\\"")&&strstr(payload,"\\"reason\\":\\"DEADLINE\\"")&&
    completed[0].progress!=last_deadline_report){
  distinct_deadline_reports++;
  last_deadline_report=completed[0].progress;
 }
 supervisions++;return 0;''')
    start=harness.index(' assert(loops==60&&reads[0]==60')
    end=harness.index(' printf(',start)
    harness=harness[:start]+f'''
 assert(loops==60&&reads[0]==60);
 assert(first_restricted_iteration=={first});
 assert(restricted_attempts=={int(bool(first))}&&moving_attempts=={moving});
 assert(distinct_deadline_reports=={60-moving});
'''+harness[end:]
    result=compile_main(tmp_path,harness,['CONFIG_XENOVA_DRIVE_WARM=1','CONFIG_XENOVA_DRIVE_LATTICE=1'])
    assert result['restricted_attempts']==int(bool(first))


def test_actual_main_reports_exit_without_offering_or_using_old_auto_work(tmp_path):
    harness=HARNESS.replace(
        'out->state=DRIVE_TASK_WAITING;return 0;',
        'out->state=task==DRIVE_TASK_AUTO&&at_us>=900000?DRIVE_TASK_EXITED:DRIVE_TASK_WAITING;return 0;')
    harness=harness.replace('static const char *last_reason;',
        'static const char *last_reason; static unsigned auto_exits, manual_reports_after_exit;')
    harness=harness.replace('supervisions++;return 0;', r'''
 if(strstr(payload,"\"reason\":\"APP_EXITED\"")){
  assert(strstr(topic,"/autonomous/")&&strstr(payload,"\"phase\":\"STOPPED\"")&&at_us>=900000);
  auto_exits++;
 }
 if(at_us>=900000&&strstr(topic,"/manual/"))manual_reports_after_exit++;
 supervisions++;return 0;''')
    start=harness.index(' assert(loops==60&&reads[0]==60')
    end=harness.index(' printf(',start)
    harness=harness[:start]+'''
 assert(loops==45&&offers[0]==45&&reads[0]==45);
 assert(auto_exits>=2&&manual_reports_after_exit>=2);
'''+harness[end:]
    compile_main(tmp_path,harness,['CONFIG_XENOVA_DRIVE_WARM=1','CONFIG_XENOVA_DRIVE_LATTICE=1'])


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
