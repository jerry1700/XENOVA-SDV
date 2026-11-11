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
GEOMETRY = HERE / "jetson-course-geometry.json"
VARIANTS = (("jetson-demo", .4625), ("jetson-r40", .40), ("jetson-r45", .45),
            ("jetson-r50", .50), ("jetson-r55", .55), ("jetson-r60", .60))
ROAD_LENGTH_M = 3.15
ROAD_HEIGHT_M = 2.07
WORLD_MARGIN_M = .10
SAMPLE_M = .05
WHEELBASE_M = .20


def read_geometry(path: Path) -> dict:
    """코스 치수는 metre, 좌우 대칭 최대 조향은 degree로 읽는다."""
    fields = {"lane_width_m", "vehicle_length_m", "vehicle_width_m", "camera_height_m"}
    values = json.loads(path.read_text(encoding="utf-8"))
    optional = {"wheelbase_m", "lane_width_tolerance_m", "max_steer_deg", "tape_width_m"}
    if not isinstance(values, dict) or not fields <= values.keys() <= fields | optional:
        raise ValueError("코스 설정에는 차로 폭, 차체 길이·폭, 카메라 높이가 필요합니다")
    values.setdefault("wheelbase_m", WHEELBASE_M)
    for key, value in values.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key}에는 양의 유한한 값을 사용하세요")
    if values["vehicle_length_m"] <= values["wheelbase_m"]:
        raise ValueError("차체 길이는 축간거리보다 길어야 합니다")
    if values.get("max_steer_deg", 0) >= 90:
        raise ValueError("최대 조향각은 90도보다 작아야 합니다")
    if values.get("tape_width_m", 0) >= values["lane_width_m"]:
        raise ValueError("테이프 폭은 한 차로 폭보다 작아야 합니다")
    return values


def _point(x: float, y: float) -> list[float]:
    return [round(x, 6), round(y, 6)]


def generate(track_id: str, inner_radius: float, geometry: dict | None = None) -> dict:
    """고정 외곽 안에서 두 차로와 차량·카메라 layout을 생성한다."""
    if geometry is None:
        geometry = read_geometry(GEOMETRY)
    lane_width = geometry["lane_width_m"]
    wheelbase = geometry.get("wheelbase_m", WHEELBASE_M)
    min_turn_radius = .55
    if "max_steer_deg" in geometry:
        min_turn_radius = wheelbase / math.tan(math.radians(geometry["max_steer_deg"]))
    length = geometry["vehicle_length_m"]
    outer = inner_radius + 1.5 * lane_width
    inner_edge = inner_radius - lane_width / 2
    radius = inner_radius + lane_width / 2
    horizontal = round(ROAD_LENGTH_M - 2 * outer, 9)
    vertical = round(ROAD_HEIGHT_M - 2 * outer, 9)
    if not math.isfinite(inner_radius) or inner_edge <= 0 or vertical < 0:
        raise ValueError("안쪽 차로 중심 반경이 315×207 cm의 두 차로 코스에 맞지 않습니다")
    tx = cy = WORLD_MARGIN_M + outer
    rx, top_cy = tx + horizontal, cy + vertical
    # 여섯 코스의 하단 직선에 공통으로 들어오는 출발 위치다.
    start_x = WORLD_MARGIN_M + 2.04
    divider_y = WORLD_MARGIN_M + lane_width
    inner_lane_y = WORLD_MARGIN_M + 1.5 * lane_width
    outer_lane_y = WORLD_MARGIN_M + lane_width / 2
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
        "start_pose": [*_point(start_x + (length + wheelbase) / 2, inner_lane_y), math.pi],
        "paved_areas": [apron, *[slot["outline"] for slot in slots]],
        "paint_lines": [*[slot["outline"] + [slot["outline"][0]] for slot in slots],
                        [_point(mouth, south), _point(inner_east(south), south)],
                        [_point(mouth, north), _point(inner_east(north), north)]],
        "parking_slots": slots,
        "obstacle_seeds": [
            {"id": "A", "kind": "cone", "x": 1.44, "y": round(inner_lane_y, 6), "w": .18, "h": .18},
            {"id": "B", "kind": "cone", "x": 1.05, "y": round(outer_lane_y, 6), "w": .18, "h": .18},
        ],
        "vehicle": {"length_m": length, "width_m": geometry["vehicle_width_m"],
                    "wheelbase_m": wheelbase, "min_turn_radius_m": min_turn_radius},
        "camera": {"height_m": geometry["camera_height_m"], "pitch_rad": math.pi / 12, "hfov_rad": 69 * math.pi / 180,
                   "mount_x_m": .20},
        "assumptions": (f"Measured road envelope 3.15 by 2.07 m; both lanes are {lane_width:g} m wide and clockwise. "
                        f"Inner lane centre radius {inner_radius:.4f} m; the default radius is estimated from the image. "
                        "Parking dimensions and positions, vehicle and camera values are provisional. "
                        "Symmetric front/rear overhang is assumed; "
                        "the rear-axle initial pose places the front bumper on the start line. "
                        "Parking bays move in x to stay inside the road loop as the radius changes."),
    }
    if "tape_width_m" in geometry:
        layout["tape_width_m"] = geometry["tape_width_m"]
    role = "기본" if track_id == "jetson-demo" else "비교"
    name = f"Jetson {role} 코스 (R{inner_radius * 100:g}cm)"
    return {"id": track_id, "name": name, "short": track_id,
            "real_m": round(lap), "centerline": points,
            "lane_width_m": 2 * lane_width, "curb_m": 0, "layout": layout}


def generated_bytes(geometry: dict | None = None) -> bytes:
    if geometry is None:
        geometry = read_geometry(GEOMETRY)
    tracks = [generate(track_id, radius, geometry) for track_id, radius in VARIANTS]
    return (json.dumps(tracks, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="기존 생성 파일과 비교만 한다")
    parser.add_argument("--geometry", type=Path, default=GEOMETRY, help="차로·차체·카메라 설정 JSON")
    parser.add_argument("--output", type=Path, default=TARGET, help="생성할 코스 JSON")
    args = parser.parse_args()
    try:
        content = generated_bytes(read_geometry(args.geometry))
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != content:
            print(f"생성 파일이 최신이 아닙니다: {args.output}")
            return 1
    else:
        args.output.write_bytes(content)
    print(f"{args.output.name}: {len(VARIANTS)} tracks, {len(content)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
