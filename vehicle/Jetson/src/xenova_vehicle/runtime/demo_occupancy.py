"""Build the outbound occupancy view for the explicitly selected demo course."""

from ..mapping.local_map import FREE, OCCUPIED, UNKNOWN
from ..mapping.registration import transform_point


def fill_demo_road(snapshot, road_mask, *, pose, camera, targets, camera_hits):
    """Fill the permitted lane polygon, reserving observed obstacles and their shadows.

    The copy is for World publication. Existing obstacle cells and their ages
    survive, and former obstacle cells stay reserved until a camera update.
    """
    import cv2
    import numpy as np

    arrays = snapshot['arrays']
    if not targets:
        raise ValueError('demo road fill needs an obstacle footprint')
    state = arrays['state'].copy()
    age = arrays['age_ms'].copy()
    occupied = state == OCCUPIED
    # The ToF overlay already reserves its own cells. Only OAK hits describe
    # what could hide the floor in the OAK image.
    hits = camera_hits & (state != FREE)
    reserved = occupied | hits
    if hits.any():
        spec = snapshot['spec']
        cell = spec.resolution_m
        half = cell / 2
        camera_x, camera_y = transform_point(camera.translation[:2], (pose.x, pose.y, pose.yaw))
        rows, cols = np.indices(state.shape)
        x = spec.origin_xy[0] + (cols + .5) * cell
        y = spec.origin_xy[1] + (rows + .5) * cell
        diameter = 2 * max(target.max_extent_m for target in targets)
        count, _, stats, _ = cv2.connectedComponentsWithStats(hits.astype(np.uint8), connectivity=8)
        dx, dy = x - camera_x, y - camera_y
        for col, row, width, height, _ in stats[1:count]:
            x0 = spec.origin_xy[0] + col * cell
            x1 = x0 + width * cell
            y0 = spec.origin_xy[1] + row * cell
            y1 = y0 + height * cell
            cx, cy = (x0+x1)/2 - camera_x, (y0+y1)/2 - camera_y
            # The first camera hit is on a visible face. Include the possible
            # unseen extent behind that face and on either side. Subtract
            # only the minimum span known from the occupied grid cells.
            px = max(0., diameter - max(0, width-2) * cell)
            py = max(0., diameter - max(0, height-2) * cell)
            x0 -= (px if cx < 0 or abs(cy) >= abs(cx) else 0.) + half
            x1 += (px if cx >= 0 or abs(cy) >= abs(cx) else 0.) + half
            y0 -= (py if cy < 0 or abs(cx) >= abs(cy) else 0.) + half
            y1 += (py if cy >= 0 or abs(cx) >= abs(cy) else 0.) + half
            reserved |= (x0 <= x) & (x <= x1) & (y0 <= y) & (y <= y1)
            # Reserve the entire sightline behind the expanded footprint.
            # This also covers any height that could hide the floor behind it.
            enter, leave = np.zeros_like(x), np.ones_like(x)
            possible = np.ones_like(state, dtype=bool)
            for delta, origin, lo, hi in ((dx,camera_x,x0,x1),(dy,camera_y,y0,y1)):
                parallel = np.abs(delta) < 1e-12
                possible &= ~parallel | (lo <= origin <= hi)
                safe = np.where(parallel,1.,delta)
                a,b = (lo-origin)/safe,(hi-origin)/safe
                enter = np.maximum(enter,np.where(parallel,-np.inf,np.minimum(a,b)))
                leave = np.minimum(leave,np.where(parallel,np.inf,np.maximum(a,b)))
            forward = dx*cx+dy*cy > 0
            reserved |= possible & forward & (enter <= leave)
    masked = reserved & ~occupied
    state[masked] = UNKNOWN
    age[masked & ~hits] = np.iinfo(np.uint32).max
    # The demo assumption is evaluated for this message, including cells
    # whose earlier camera FREE has aged out on the Pi side.
    # Pi intersects whole cells and permits the body to extend past local
    # lane end caps. The demo's collision grid covers a 20cm band around
    # the cut; the nominal road mask still defines road priority.
    rings = max(1, round(.20 / snapshot['spec'].resolution_m))
    grid_window = cv2.dilate(road_mask.astype(np.uint8), np.ones((3,3),np.uint8), iterations=rings).astype(bool)
    filled = grid_window & arrays['support'] & arrays['permission'] & ~reserved
    state[filled] = FREE
    age[filled] = 0
    state.setflags(write=False)
    age.setflags(write=False)
    counts = dict(unknown=int((state == UNKNOWN).sum()),free=int((state == FREE).sum()),
                  occupied=int((state == OCCUPIED).sum()))
    return dict(snapshot, arrays=dict(arrays,state=state,age_ms=age), counts=counts)
