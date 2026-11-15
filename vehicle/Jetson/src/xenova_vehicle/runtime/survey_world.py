"""Publish the surveyed road through the ordinary mapper World encoder."""

from hashlib import sha256
import math
import struct

from ..transport.world_adapter import WorldBuildError, build_world


def build_survey_world(snapshot, value, *, source, course, pose, crossing, static_retained=None):
    """Cut a local road window from the survey and preserve the evidence grid."""
    import numpy as np

    station = course.project(pose[0], pose[1])
    dx, dy, heading = course.pose(station, 0)
    if (math.hypot(pose[0]-dx, pose[1]-dy) > course.lane_width_m
            or abs(math.remainder(pose[2]-heading, math.tau)) > math.pi/3):
        raise WorldBuildError('localized vehicle does not match the surveyed course')
    original = value.lane_model
    if len(original.divider) < 2 or len(original.lanes) != 2 or any(not lane.centerline for lane in original.lanes):
        raise WorldBuildError('survey publication requires the two admitted lane identities')
    a, b = original.divider[:2]
    lane_branches = [1 if (b.x-a.x)*(lane.centerline[0].y-a.y) > (b.y-a.y)*(lane.centerline[0].x-a.x) else -1
                     for lane in original.lanes]
    if sorted(lane_branches) != [-1,1]:
        raise WorldBuildError('the original two lane identities must straddle the divider')
    lane_stations = course.window(station)
    hard_stations = list(lane_stations)
    while len(hard_stations) < 48:
        i = max(range(len(hard_stations)-1), key=lambda i: hard_stations[i+1]-hard_stations[i])
        hard_stations.insert(i+1, (hard_stations[i]+hard_stations[i+1])/2)
    basis_stations = [-.75+(course.length-.1)*i/47 for i in range(48)]
    for points, branch, positions in (
            (value.reference, 0, basis_stations),
            (value.left_boundary, 2, hard_stations),
            (value.right_boundary, -2, hard_stations),
            (value.lane_model.divider, 0, lane_stations),
            *((lane.centerline, branch, lane_stations) for lane, branch in zip(original.lanes, lane_branches))):
        del points[:]
        for s in positions:
            x, y, _ = course.pose(s, branch)
            points.add(x=x, y=y)
    digest = sha256(b''.join(struct.pack('<ff', p.x, p.y) for p in value.reference)).hexdigest()
    value.reference_id, value.reference_revision = 'survey:'+digest[:32], 1
    # The surveyed stripe roles and crossing policy are pinned for the course.
    model = value.lane_model
    model.crossing = bytes([int(crossing)])*(len(lane_stations)-1)
    model.divider_state = bytes(len(lane_stations))
    for lane in model.lanes:
        lane.point_state = bytes(len(lane_stations))
    preferred = np.asarray(snapshot['arrays']['support'], dtype=bool)
    # A survey supplies geometry only. The source grid still owns FREE, HIT
    # and unknown cells, permission, timestamps, and subcell proofs.
    return build_world(snapshot, value, source=source, preferred_road=preferred, static_retained=static_retained)
