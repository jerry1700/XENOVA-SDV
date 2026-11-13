"""한 RGB의 정합 후보 순서, 탐색 예산, 채택 규칙을 관리한다."""

from dataclasses import dataclass, replace
import math

from ..mapping.registration import FrozenReference, PointFeature, RegistrationResult, overlapping_points, register, wrap_yaw
from .registration_history import combined_constraints


@dataclass(frozen=True)
class VisualRegistrationPolicy:
    """탐색 해상도와 독립적인 보정 허용치 및 재방문 관측 설정."""

    single_frame_shift_m: float = .01
    single_frame_turn_rad: float = .01
    revisit_trigger_fraction: float = .65
    revisit_min_age_us: int = 4_000_000
    revisit_history_us: int = 700_000
    revisit_min_improvement_m: float = .001
    revisit_max_cells: int = 1024

    def __post_init__(self):
        for name, upper in (('single_frame_shift_m', .2), ('single_frame_turn_rad', .5),
                            ('revisit_trigger_fraction', 1.), ('revisit_min_improvement_m', .05)):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= upper:
                raise ValueError('invalid visual registration setting: '+name)
        for name, upper in (('revisit_min_age_us', 60_000_000), ('revisit_history_us', 1_000_000),
                            ('revisit_max_cells', 4096)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError('invalid visual registration setting: '+name)


@dataclass(frozen=True)
class RegistrationDecision:
    result: RegistrationResult
    method: str | None
    points: tuple[PointFeature, ...]
    reference: FrozenReference

    def conflicts_with(self, pose, profile, policy):
        """한 번의 약한 정합으로 유효한 전파 자세를 바꾸는지 판정한다."""
        fit = self.result
        sample = pose.sample
        if fit.status == 'BOUNDED':
            if self.method not in ('PHYSICAL', 'NORMAL', 'MIXED') or (
                    sample.position_error_m > profile.max_position_error_m
                    or sample.yaw_error_rad > profile.max_yaw_error_rad):
                return False
            return (math.dist(fit.representative[:2], (sample.x, sample.y)) > policy.single_frame_shift_m
                    or abs(wrap_yaw(fit.representative[2]-sample.yaw)) > policy.single_frame_turn_rad)
        return (fit.status != 'NO_OVERLAP' and any(not p.normal_only for p in self.points)
                and (fit.status != 'AMBIGUOUS_OR_UNOBSERVABLE' or not fit.permits(pose.prior.center)))


class _SearchBudget:
    """선택 결과와 무관하게 실행한 모든 solver 작업을 원 frame 한도에서 차감한다."""

    def __init__(self, profile):
        self.profile = profile
        self.cells = self.tests = 0

    @property
    def available(self):
        return self.cells < self.profile.max_cells and self.tests < self.profile.max_distance_tests

    def solve(self, points, reference, prior, *, position_radius_m, max_cells=None, min_steps=None):
        remaining = self.profile.max_cells-self.cells
        steps = self.profile.cell_steps
        if min_steps is not None:
            steps = tuple(max(a,b) for a,b in zip(steps,min_steps))
        fit = register(points,reference,prior,replace(self.profile,
            max_cells=remaining if max_cells is None else min(remaining,max_cells),
            max_distance_tests=self.profile.max_distance_tests-self.tests,cell_steps=steps),
            calibration_id=reference.calibration_id,position_radius_m=position_radius_m)
        self.cells += fit.tested_cells
        self.tests += fit.distance_tests
        return fit


def select_registration(current, reference, prediction, pose, profile, policy, *,
                        history, history_age_us, lane_width_m, revisit=None):
    """후보 선택만 한다. 지도·기준점·관측 시각의 갱신은 호출자 소유다."""
    prior = prediction.prior
    radius = prediction.sample.position_error_m
    lateral = lane_width_m / 2
    if reference.feature_policy != 'OBSERVED_STRIP_SUPPORT_V1':
        reference = replace(reference, feature_policy='OBSERVED_STRIP_SUPPORT_V1',
            segments=tuple(replace(s,component=s.capture_id+':'+s.component)
                           for s in reference.segments if s.a != s.b))
    common = overlapping_points(current.points,reference,prior,profile,observed_ends=dict(current.ends),
                                position_radius_m=radius,max_lateral_match_m=lateral)
    selected = RegistrationDecision(
        RegistrationResult('NO_OVERLAP',None,None,None,(prior,),reference.revision,0,0,True),
        None,common,reference)
    budget = _SearchBudget(profile)
    if common:
        physical = tuple(p for p in common if not p.normal_only)
        fit = budget.solve(physical or common,reference,prior,position_radius_m=radius)
        selected = RegistrationDecision(fit,'PHYSICAL' if physical else 'NORMAL',common,reference)
        if physical and len(physical) != len(common) and fit.status not in ('BOUNDED','INCONSISTENT') and budget.available:
            mixed = budget.solve(common,reference,prior,position_radius_m=radius)
            if mixed.status == 'BOUNDED':
                selected = RegistrationDecision(mixed,'MIXED',common,reference)
        if history and selected.result.status not in ('INCONSISTENT','UNKNOWN_BUDGET') and budget.available:
            points, older = combined_constraints(history,current,reference,prior,profile,position_radius_m=radius,
                max_lateral_match_m=lateral,max_age_us=history_age_us)
            if points:
                joint = budget.solve(points,older,prior,position_radius_m=radius)
                fit = selected.result
                if (joint.status == 'BOUNDED' and (not fit.permits(prior.center) or joint.permits(prior.center))
                        and (fit.status != 'BOUNDED' or _improves(joint,fit))):
                    selected = RegistrationDecision(joint,'MULTI_FRAME',points,older)
    if revisit is not None and budget.available and (
            pose.sample.position_error_m >= policy.revisit_trigger_fraction*profile.max_position_error_m
            or pose.sample.yaw_error_rad >= policy.revisit_trigger_fraction*profile.max_yaw_error_rad):
        candidate = revisit(budget.solve,reference)
        if candidate is not None:
            fit, points, local_reference = candidate
            original = selected.result
            if original.status != 'BOUNDED' or (
                    fit.position_error_m < original.position_error_m and fit.yaw_error_rad <= original.yaw_error_rad
                    and original.permits(fit.representative)):
                selected = RegistrationDecision(fit,'MAPPED_REVISIT',points,local_reference)
    return replace(selected,result=replace(selected.result,tested_cells=budget.cells,distance_tests=budget.tests))


def _improves(candidate, previous):
    return (candidate.position_error_m <= previous.position_error_m and candidate.yaw_error_rad <= previous.yaw_error_rad
            and (candidate.position_error_m < previous.position_error_m or candidate.yaw_error_rad < previous.yaw_error_rad))
