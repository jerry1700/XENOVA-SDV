"""실측 도로 외곽과 사진을 바탕으로 Jetson 코스 6종의 공유 geometry를 생성한다.

좌표는 metre, world XY-up이다. 중심선은 도로 가운데 분리선이며 시계 방향이다.
이 데이터는 주차장 표현용이고 차량 기동 가능성이나 실측 보정을 보증하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parents[3] / "frontend/packages/ui/src/sim/jetson-tracks.json"
VARIANTS = (("jetson-demo", .4625), ("jetson-r40", .40), ("jetson-r45", .45),
            ("jetson-r50", .50), ("jetson-r55", .55), ("jetson-r60", .60))
ROAD_LENGTH_M = 3.15
ROAD_HEIGHT_M = 2.07
LANE_WIDTH_M = .285
WORLD_MARGIN_M = .10
SAMPLE_M = .05


def _point(x: float, y: float) -> list[float]:
    return [round(x, 6), round(y, 6)]


def generate(track_id: str, inner_radius: float) -> dict:
    """도로 외곽 315×207 cm와 두 차로 폭을 고정한다."""
    outer = inner_radius + 1.5 * LANE_WIDTH_M
    inner_edge = inner_radius - LANE_WIDTH_M / 2
    radius = inner_radius + LANE_WIDTH_M / 2
    horizontal = round(ROAD_LENGTH_M - 2 * outer, 9)
    vertical = round(ROAD_HEIGHT_M - 2 * outer, 9)
    if not math.isfinite(inner_radius) or inner_edge <= 0 or vertical < 0:
        raise ValueError("안쪽 차로 중심 반경이 315×207 cm의 두 차로 코스에 맞지 않습니다")
    tx = cy = WORLD_MARGIN_M + outer
    rx, top_cy = tx + horizontal, cy + vertical
    # 여섯 코스의 하단 직선에 공통으로 들어오는 출발 위치다.
    start_x = WORLD_MARGIN_M + 2.04
    divider_y = WORLD_MARGIN_M + LANE_WIDTH_M
    inner_lane_y = WORLD_MARGIN_M + 1.5 * LANE_WIDTH_M
    outer_lane_y = WORLD_MARGIN_M + LANE_WIDTH_M / 2
    points = [_point(start_x, divider_y)]

    def line(x: float, y: float) -> None:
        ax, ay = points[-1]
        distance = math.hypot(x - ax, y - ay)
        if distance < 1e-9:
            return
        count = max(1, math.ceil(distance / SAMPLE_M))
        points.extend(_point(ax + (x - ax) * i / count, ay + (y - ay) * i / count)
                      for i in range(1, count + 1))

    def arc(cx: float, center_y: float, a: float, b: float) -> None:
        count = max(1, math.ceil(radius * abs(b - a) / SAMPLE_M))
        points.extend(_point(cx + radius * math.cos(a + (b - a) * i / count),
                             center_y + radius * math.sin(a + (b - a) * i / count))
                      for i in range(1, count + 1))

    line(tx, divider_y)
    arc(tx, cy, -math.pi / 2, -math.pi)
    line(tx - radius, top_cy)
    arc(tx, top_cy, math.pi, math.pi / 2)
    line(rx, top_cy + radius)
    arc(rx, top_cy, math.pi / 2, 0)
    line(rx + radius, cy)
    arc(rx, cy, 0, -math.pi / 2)
    line(start_x, divider_y)
    points.pop()  # 마지막 점은 첫 점과 같으므로 고리 연결에만 사용한다.
    lap = sum(math.dist(a, points[(i + 1) % len(points)]) for i, a in enumerate(points))
    # 사진의 오른쪽 안마당에 세로로 두 칸을 배치한다. 입구는 오른쪽 차로 쪽이다.
    slot_width, slot_depth = .30, .46
    south = WORLD_MARGIN_M + .67
    north = south + 2 * slot_width

    def inner_east(y: float) -> float:
        dy = max(cy - y, y - top_cy, 0.)
        return rx + math.sqrt(inner_edge ** 2 - dy ** 2)

    # 두 칸을 곡선 안쪽에 놓고, 연결 마당은 실제 도로와 겹치게 한다.
    mouth = min(inner_east(south), inner_east(north)) - .01
    back = mouth - slot_depth
    apron_east = rx + inner_edge + .015
    slots = []
    for i in range(2):
        y = south + i * slot_width
        slots.append({"id": f"P{i + 1}", "outline": [_point(back, y), _point(back, y + slot_width),
                      _point(mouth, y + slot_width), _point(mouth, y)]})
    apron = [_point(mouth, south), _point(apron_east, south),
             _point(apron_east, north), _point(mouth, north)]
    layout = {
        "bounds": [0, 0, *_point(ROAD_LENGTH_M + 2 * WORLD_MARGIN_M, ROAD_HEIGHT_M + 2 * WORLD_MARGIN_M)],
        "lane_count": 2,
        "start_pose": [*_point(start_x + (.30 + .20) / 2, inner_lane_y), math.pi],
        "paved_areas": [apron, *[slot["outline"] for slot in slots]],
        "paint_lines": [*[slot["outline"] + [slot["outline"][0]] for slot in slots],
                        [_point(mouth, south), _point(inner_east(south), south)],
                        [_point(mouth, north), _point(inner_east(north), north)]],
        "parking_slots": slots,
        "obstacle_seeds": [
            {"id": "A", "kind": "cone", "x": 1.44, "y": round(inner_lane_y, 6), "w": .18, "h": .18},
            {"id": "B", "kind": "cone", "x": 1.05, "y": round(outer_lane_y, 6), "w": .18, "h": .18},
        ],
        "vehicle": {"length_m": .30, "width_m": .22, "wheelbase_m": .20, "min_turn_radius_m": .55},
        "camera": {"height_m": .12, "pitch_rad": math.pi / 12, "hfov_rad": 69 * math.pi / 180,
                   "mount_x_m": .20},
        "assumptions": (f"Measured road envelope 3.15 by 2.07 m; both lanes are 0.285 m wide and clockwise. "
                        f"Inner lane centre radius {inner_radius:.4f} m; the default radius is estimated from the image. "
                        "Parking dimensions and positions, vehicle and camera values are provisional. "
                        "Symmetric front/rear overhang is assumed; "
                        "the rear-axle initial pose places the front bumper on the start line. "
                        "Parking bays move in x to stay inside the road loop as the radius changes."),
    }
    role = "기본" if track_id == "jetson-demo" else "비교"
    name = f"Jetson {role} 코스 (R{inner_radius * 100:g}cm)"
    return {"id": track_id, "name": name, "short": track_id,
            "real_m": round(lap), "centerline": points,
            "lane_width_m": 2 * LANE_WIDTH_M, "curb_m": 0, "layout": layout}


def generated_bytes() -> bytes:
    return (json.dumps([generate(*variant) for variant in VARIANTS], ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="기존 생성 파일과 비교만 한다")
    args = parser.parse_args()
    content = generated_bytes()
    if args.check:
        if not TARGET.exists() or TARGET.read_bytes() != content:
            print(f"생성 파일이 최신이 아닙니다: {TARGET}")
            return 1
    else:
        TARGET.write_bytes(content)
    print(f"{TARGET.name}: {len(VARIANTS)} tracks, {len(content)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
