"""명시 설치 route budget의 root 소유 private JSON을 읽는다.

실측 기본값·기능 광고·출발 권한을 만들지 않는다. 등록 digest는 실제 읽은 값의
기존 canonical JSON bytes에서 계산하며 profile이 자기를 승인하는 hash 필드는 없다.
"""
from hashlib import sha256

from .authority_journal import canonical
from .operator_profile import private_profile
from .route_policy import RouteBudgetPolicy


def load_route(path,*,uid=0):
    """정확13개 budget/hash 필드를 검산한다. 생략·추가·미측정 기본값은 허용하지 않는다."""
    fields=set(RouteBudgetPolicy.__dataclass_fields__)-{'registered_sha256'}
    value=private_profile(path,fields,uid=uid)
    registered=sha256(canonical(value)).digest()
    typed=dict(value)
    for key in ('required_extent_policy_sha256','terminal_profile_sha256'):
        text=typed[key]
        if type(text) is not str or len(text)!=64 or any(c not in '0123456789abcdef' for c in text):
            raise ValueError('installed route profile digest')
        typed[key]=bytes.fromhex(text)
    return RouteBudgetPolicy.from_registered(typed,registered)
