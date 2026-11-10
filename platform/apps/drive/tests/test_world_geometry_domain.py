"""새 road basis와 global hard domain의 Python/C 동일 수락 경계.

기하·시각은 명시 software fixture다. 실제 source/scene/통행 승인은 만들지 않는다.
공용 실제 C decoder를 ASan/UBSan으로 실행하며 legacy side 정책도 별도로 보존한다.
"""

import pytest

from xenova_drive_world import decode,encode,schema
from xenova_drive_world import codec
from test_world import probe,product,fixture,parking_world
from test_world import modern_world


def world(features):
    value=parking_world() if features==15 else modern_world() if features==11 else fixture.world()
    value.sequence=2
    value.ClearField('reference');value.ClearField('left_boundary');value.ClearField('right_boundary')
    for x,y in ((1.,1.),(3.,1.),(3.,3.),(1.,3.)):
        value.reference.add(x=x,y=y)
    # actual global rectangle의 두 chain. basis의 마지막 서향 구간과 독립이다.
    for x in (0.,4./3,8./3,4.):
        value.left_boundary.add(x=x,y=4.)
        value.right_boundary.add(x=x,y=0.)
    return value


@pytest.mark.parametrize('features',[11,15])
def test_open_curved_basis_does_not_reinterpret_global_domain_as_lane_sides(probe,features):
    value=world(features)
    wire=encode(value)
    assert product(probe,wire)['result']=='OK'


@pytest.mark.parametrize('features',[0,1])
def test_legacy_per_station_side_rule_is_retained(probe,features):
    value=world(0)
    if features==1:
        value.required_features=1;value.cell_flags=bytes([9])*(value.width*value.height)
    assert product(probe,value.SerializeToString())['result']=='GEOMETRY'


@pytest.mark.parametrize('kind',['SELF_CROSSING_DOMAIN','CLOSED_BASIS','SELF_CROSSING_BASIS'])
def test_new_geometry_remains_closed_to_crossings_and_revisited_station(probe,kind):
    value=world(11)
    if kind=='SELF_CROSSING_DOMAIN':
        value.left_boundary[1].x,value.left_boundary[1].y=3.,-1.
    else:
        points=((1.,1.),(3.,1.),(3.,3.),(1.,1.)) if kind=='CLOSED_BASIS' else ((1.,1.),(3.,3.),(1.,3.),(3.,1.))
        for target,(x,y) in zip(value.reference,points):target.x,target.y=x,y
    with pytest.raises(ValueError):encode(value)
    assert product(probe,value.SerializeToString())['result']=='GEOMETRY'


@pytest.mark.parametrize('points',[((1.,1.),(2.,1.),(1.,1.)),((1.,1.),(3.,1.),(2.,1.))])
def test_two_adjacent_edges_cannot_close_or_retrace_station(probe,points):
    value=world(11)
    for field in ('reference','left_boundary','right_boundary'):value.ClearField(field)
    for (x,y),boundary_x in zip(points,(0.,2.,4.)):
        value.reference.add(x=x,y=y)
        value.left_boundary.add(x=boundary_x,y=4.)
        value.right_boundary.add(x=boundary_x,y=0.)
    with pytest.raises(ValueError):encode(value)
    assert product(probe,value.SerializeToString())['result']=='GEOMETRY'


def test_forward_collinear_stations_are_preserved(probe):
    value=world(11)
    for field in ('reference','left_boundary','right_boundary'):value.ClearField(field)
    for x,boundary_x in zip((1.,2.,3.),(0.,2.,4.)):
        value.reference.add(x=x,y=1.)
        value.left_boundary.add(x=boundary_x,y=4.)
        value.right_boundary.add(x=boundary_x,y=0.)
    assert product(probe,encode(value))['result']=='OK'


@pytest.fixture
def simple_geometry_cache():
    codec._simple_success.cache_clear()
    yield
    codec._simple_success.cache_clear()


def task_geometry(points=None):
    value=schema.TaskReferenceState(home_lane_id=2,from_lane_id=1,selected_lane_id=1,
        changing=False,basis_revision=1,vehicle='rc-test-1',mapper_boot=fixture.BOOT,
        mapper_instance=fixture.INSTANCE,clock_epoch=fixture.CLOCK)
    for x,y in (points if points is not None else [(i*.1,1.6) for i in range(64)]):
        value.basis_prefix.add(x=x,y=y)
    return value


def count_intersections(monkeypatch):
    calls=[];original=codec._intersects
    def checked(*points):
        calls.append(1)
        return original(*points)
    monkeypatch.setattr(codec,'_intersects',checked)
    return calls


def test_repeated_task_codec_keeps_exact_wire_without_repeating_simplicity_work(simple_geometry_cache,monkeypatch):
    calls=count_intersections(monkeypatch);value=task_geometry()
    raw=encode(value);first=len(calls)
    assert first>0
    assert decode(raw,schema.TaskReferenceState)==value
    assert encode(value)==raw and len(calls)==first


def test_close_coordinate_mutation_and_repeated_crossing_never_reuse_success(simple_geometry_cache):
    value=task_geometry([(0.,0.),(1.1e-6,0.),(1.,0.)]);encode(value)
    original_size=codec._simple_success.cache_info().currsize
    value.basis_prefix[1].x=.9e-6  # 정밀도를 줄이면 같은 key가 될 수 있는 별개 입력.
    with pytest.raises(ValueError,match='degenerate'):encode(value)
    assert codec._simple_success.cache_info().currsize==original_size
    valid=task_geometry([(0.,0.),(2.,0.),(2.,2.),(0.,2.)]);encode(valid)
    bad=task_geometry([(0.,0.),(2.,2.),(0.,2.),(2.,0.)])
    before=codec._simple_success.cache_info()
    for _ in range(2):
        with pytest.raises(ValueError,match='self-intersecting'):encode(bad)
    after=codec._simple_success.cache_info()
    assert after.currsize==before.currsize and after.misses==before.misses+2


def test_closed_edge_is_part_of_exact_simplicity_key(simple_geometry_cache):
    points=[(0.,0.),(2.,0.),(0.,2.),(2.,2.)]
    codec._simple(points,False)
    with pytest.raises(ValueError,match='self-intersecting'):codec._simple(points,True)
    codec._simple(points,False)
    assert codec._simple_success.cache_info().currsize==1


def test_success_cache_is_bounded_and_above128_uses_original_check(simple_geometry_cache,monkeypatch):
    calls=count_intersections(monkeypatch);points=[(float(i),0.) for i in range(128)]
    codec._simple(points);first=len(calls)
    codec._simple(list(points));assert len(calls)==first
    for offset in range(9):
        y=float(offset+1)
        codec._simple([(0.,y),(1.,y),(1.,y+1.),(2.,y+1.)])
    assert codec._simple_success.cache_info().maxsize==8
    assert codec._simple_success.cache_info().currsize==8
    previous=len(calls);codec._simple(points);assert len(calls)>previous
    before=codec._simple_success.cache_info();large=points+[(128.,0.)]
    for _ in range(2):
        previous=len(calls);codec._simple(large);assert len(calls)>previous
    assert codec._simple_success.cache_info()==before


@pytest.mark.parametrize('invalid',['nan','inf','required','revision','source','duplicate','cap'])
def test_warm_geometry_never_bypasses_current_message_checks(simple_geometry_cache,invalid):
    value=task_geometry();raw=encode(value)
    if invalid=='duplicate':
        with pytest.raises(ValueError,match='duplicate'):decode(raw+b'\x08\x02',schema.TaskReferenceState)
        return
    if invalid in ('nan','inf'):value.basis_prefix[3].x=float(invalid)
    elif invalid=='required':value.ClearField('home_lane_id')
    elif invalid=='revision':value.basis_revision=0
    elif invalid=='source':value.mapper_boot='invalid-source'
    else:value.basis_prefix.add(x=8.,y=1.6)
    with pytest.raises(ValueError):encode(value)
