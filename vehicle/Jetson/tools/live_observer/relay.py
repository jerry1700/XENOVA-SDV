"""SSH의 읽기 전용 NDJSON을 PC 최신 파일로 옮긴다. 인증·제품 통신을 소유하지 않는다."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from .collector import SOURCE_CAP, atomic, packed


def relay(source, output):
    """최대8개 topic/응답 파일을 원자 교체한다. 재실행은 별도 relay epoch다."""
    directory = Path(output);directory.mkdir(parents=True,exist_ok=True)
    epoch = str(uuid4());sequence = 0;keys = set();write_errors = 0
    while True:
        raw = source.readline(SOURCE_CAP+1)
        if not raw:return
        if len(raw)>SOURCE_CAP:
            while raw and not raw.endswith(b'\n'):raw=source.readline(SOURCE_CAP+1)
            value = dict(kind='error',reason='REMOTE_LINE_EXCEEDS_LIMIT')
        else:
            try:
                value = json.loads(raw)
                if type(value) is not dict or value.get('kind') not in ('mqtt','operator','error'):continue
            except (ValueError,UnicodeError):value=dict(kind='error',reason='REMOTE_LINE_INVALID')
        key = value.get('topic') if value['kind']=='mqtt' else value['kind']
        if type(key) is not str or len(key)>256:continue
        if key not in keys and len(keys)>=8:continue
        keys.add(key);sequence+=1
        body = dict(schema='pc-remote-observation-1',relay_epoch=epoch,sequence=sequence,
                    received_pc_us=time.monotonic_ns()//1000,payload=value,write_errors=write_errors)
        raw = packed(body)
        if len(raw)>SOURCE_CAP:continue
        try:atomic(directory/('remote-'+sha256(key.encode()).hexdigest()[:16]+'.json'),raw)
        except OSError:write_errors+=1  # Windows reader의 일시 파일 점유도 관측 한 건만 버린다.


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True)
    args=parser.parse_args(argv)
    relay(sys.stdin.buffer,args.output_dir)


if __name__=='__main__':main()
