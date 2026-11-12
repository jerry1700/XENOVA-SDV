"""명시 opt-in 설치 정책. 수치를 하드코딩하거나 실측 수락으로 가장하지 않는다."""
from dataclasses import dataclass,asdict,field
import json
from types import MappingProxyType
import os
from pathlib import Path
import stat

from .manual_source import ManualProfile
from .guest_clock import GuestClockPolicy
from .feedback3 import Feedback3Profile
from .scene_authority import StationaryPolicy


def private_profile(path,fields,*,uid=0,optional=()):
    from .ipc_contract import decode
    target=Path(path)
    if not target.is_absolute() or target.resolve()!=target:raise ValueError('canonical installed policy path')
    fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_mode&0o077 or info.st_nlink!=1 or info.st_size>8192:
            raise ValueError('private owned single-link bounded policy')
        value=decode(stream.read(8193))
    if (type(value) is not dict or not set(fields)<=set(value)
            or not set(value)<=set(fields)|set(optional)):raise ValueError('exact installed policy fields')
    return value


@dataclass(frozen=True)
class OperatorProfile:
    manual: ManualProfile
    guest_clock: GuestClockPolicy
    keyboard_profile_directory: str
    planner_manifest: str
    planner_asset_sha256: str
    episode_support: bool = False
    operation_support: bool = False
    operation_parking_strategy: str = 'REVERSE'
    planner_parameters: object = field(default=None,init=False,repr=False,compare=False)

    @classmethod
    def load(cls,path,*,uid=0):
        fields={name for name,item in cls.__dataclass_fields__.items() if item.init}
        optional=('episode_support','operation_support','operation_parking_strategy')
        value=private_profile(path,fields-set(optional),uid=uid,optional=optional)
        if type(value.get('episode_support',False)) is not bool:raise ValueError('explicit episode installation approval')
        if type(value.get('operation_support',False)) is not bool:raise ValueError('explicit operation installation approval')
        if value.get('operation_support',False) and not value.get('episode_support',False):raise ValueError('operation requires approved episode profile')
        if value.get('operation_parking_strategy','REVERSE') not in ('REVERSE','FORWARD'):raise ValueError('installed parking strategy')
        if set(value['manual'])!=set(ManualProfile.__dataclass_fields__) or set(value['guest_clock'])!=set(GuestClockPolicy.__dataclass_fields__):
            raise ValueError('exact manual/guest policy')
        directory=Path(value['keyboard_profile_directory'])
        if not directory.is_absolute() or directory.resolve()!=directory:raise ValueError('registered keyboard policy directory')
        manifest=Path(value['planner_manifest'])
        if not manifest.is_absolute() or manifest.resolve()!=manifest:raise ValueError('canonical planner manifest path')
        fd=os.open(manifest,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_mode&0o022 or info.st_nlink!=1
                    or info.st_size>32768):raise ValueError('owned read-only bounded installed planner manifest')
            raw=stream.read(32769)
        guest=GuestClockPolicy(**value['guest_clock'])
        from xenova_drive_world.planner_profile import verify_manifest
        verify_manifest(raw,value['planner_asset_sha256'],asdict(guest))
        result=cls(ManualProfile(**value['manual']),guest,str(directory),str(manifest),value['planner_asset_sha256'],
            value.get('episode_support',False),value.get('operation_support',False),value.get('operation_parking_strategy','REVERSE'))
        object.__setattr__(result,'planner_parameters',MappingProxyType(json.loads(raw)['profile']))
        return result


def load_feedback3(path,*,uid=0):
    value=private_profile(path,Feedback3Profile.__dataclass_fields__,uid=uid)
    if type(value['stationary']) is not dict or set(value['stationary'])!=set(StationaryPolicy.__dataclass_fields__):
        raise ValueError('exact stationary policy')
    return Feedback3Profile(**{**value,'stationary':StationaryPolicy(**value['stationary'])})
