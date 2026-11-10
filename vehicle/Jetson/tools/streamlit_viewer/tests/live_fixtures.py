"""PC 표시 계약의 명시 fixture. 실제 보드/collector/권한 관측으로 사용하지 않는다."""
from snapshot import SCHEMA,LIVE_KIND,LIVE_SCHEMA


def live_document(sequence=1):
    return dict(schema=SCHEMA,
        source=dict(id='declared-pc-observer',epoch='pc-collector-session',sequence=str(sequence),kind=LIVE_KIND,stamp=None),
        live=dict(schema=LIVE_SCHEMA,run_id='declared-run',session_id='declared-session',
            collector=dict(id='collector-fixture',epoch='pc-collector-session',sequence=str(sequence),
                collected_stamp=dict(domain='PC',epoch='pc-boot',us='1000000')),
            connection=dict(stage='WAITING_FOR_RUN',state='WAITING',stop_reason='NO_RUN',last_error=None),
            streams=[dict(name='camera',state='WAITING',received_count=0,source_id=None,source_epoch=None,
                source_sequence=None,source_stamp=None,pc_received_stamp=None,received_age_s=None,error=None)],
            stages={}),operation=None,map=dict(layers=[]),media=[])
