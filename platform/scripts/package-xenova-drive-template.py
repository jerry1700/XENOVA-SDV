#!/usr/bin/env python3
"""실제 LAB/development ARM 빌드를 새 불변 template로 포장한다. 보드 수락은 별개다."""
import argparse
import importlib.util
import json
import re
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'platform/first-install'),str(ROOT/'platform/yocto/meta-xenova/recipes-core/xenova-platform/files')]
import xenova_zephyr as zephyr
from artifact_io import new_path,publish
from first_install_contract import uuid4

ID='xenova-zephyr-drive-lattice-lab-1.0.0'
TEMPLATE='zephyr-drive-lattice-lab-arm64'
VERSION='1.0.8'
DEVELOPMENT_ID='xenova-zephyr-drive-development-1.0.0'
DEVELOPMENT_TEMPLATE='zephyr-drive-development-arm64'


def package(build,output,*,version=None):
    zephyr.plain(build);new_path(output)
    receipt=json.loads(zephyr.asset(build,'BUILD_RECEIPT.json').read_text())
    spec=importlib.util.spec_from_file_location('drive_builder',ROOT/'platform/scripts/build-xenova-drive-app.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    development=receipt.get('profile')=='warm-lattice-development'
    contract=receipt.get('drive_contract',{})
    bundle=build/'input'/module.DEVELOPMENT_BUNDLE if development and contract.get('explicit_development_bundle') is True else None
    if (receipt.get('app')!='drive' or receipt.get('profile') not in module.LATTICE_PROFILES
            or receipt.get('source_files')!=module.fingerprint('drive',bundle)
            or receipt.get('acceptance',{}).get('M1_arm_build') is not True):
        raise ValueError('current real drive build/profile/source receipt required')
    if version is None and development:
        raise ValueError('new explicit development release version required; legacy defaults are unchanged')
    version=VERSION if version is None else version
    if not isinstance(version,str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",version):
        raise ValueError("plain major.minor.patch firmware version required")
    profile,generated,provenance=module.lattice_inputs(receipt['profile'],bundle)
    if (contract.get('world_profile')!=profile['id'] or contract.get('primitive_sha256')!=generated['asset_sha256']
            or (development and contract.get('development_inputs')!=provenance)):
        raise ValueError('selected profile differs from actual ARM build receipt')
    bundle_id=DEVELOPMENT_ID if development else ID
    template=DEVELOPMENT_TEMPLATE if development else TEMPLATE
    source=build/'firmware';info=zephyr.bundle_info(source)
    for name,expected in receipt['files'].items():
        path=zephyr.asset(source,name)
        if expected!={'sha256':zephyr.sha(path),'bytes':path.stat().st_size}:raise ValueError('drive artifact differs from build receipt')
    meta=json.loads((source/'firmware.json').read_text())
    if meta['source']['app']!='xenova-drive' or info['provisioning_profile']['apps']!={
        'autonomous':{'model':'stateful','standby_progress':'idle'},'manual':{'model':'stateless','standby_progress':'idle'}}:
        raise ValueError('actual stateful AUTO/independent MANUAL profile required')
    stage=output.parent/('.drive-template-'+uuid4());shutil.copytree(source,stage)
    meta.update(id=bundle_id,kind='zephyr-xen-firmware',template_id=template,template_version=version,version=version)
    meta['source']['build_receipt_sha256']=zephyr.sha(build/'BUILD_RECEIPT.json')
    meta['source'].update(zephyr_commit=receipt['zephyr_commit'],sdk_version=receipt['sdk_version'],drive_profile=receipt['profile'])
    # 원 firmware/manifest를 덮지 않고 새 배포 envelope만 생성한다.
    target=stage/'firmware.json';target.chmod(0o644);target.write_text(json.dumps(meta,indent=2)+'\n');target.chmod(0o444)
    result=zephyr.bundle_info(stage);publish(stage,output)
    return {'id':bundle_id,'template_id':template,'version':version,'directory':f'zephyr/{bundle_id}/{version}',
            'manifest_sha256':result['revision'],'guest_kind':'zephyr','firmware_sha256':result['sha256']}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--build-root',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--version',help='new immutable release; required for development, LAB defaults to '+VERSION)
    args=p.parse_args(argv)
    print(json.dumps(package(args.build_root,args.output,version=args.version),indent=2))


if __name__=='__main__':main()
