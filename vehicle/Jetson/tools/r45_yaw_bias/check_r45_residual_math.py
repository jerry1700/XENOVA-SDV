"""Cross-check the trial's enclosure against exact constant-curvature integration."""

import math
from random import Random

from r45_yaw_bias_trial import ResidualBounds


def arc(distance, yaw, turn):
    half = turn / 2
    scale = math.sin(half)/half if half else 1.
    return distance*scale*math.cos(yaw+half), distance*scale*math.sin(yaw+half)


def follow(steps, initial_yaw, rate):
    x, y, yaw = 0., 0., initial_yaw
    for distance, corrected_turn, dt, applied in steps:
        turn = corrected_turn - (rate - applied)*dt
        dx, dy = arc(distance, yaw, turn)
        x, y, yaw = x+dx, y+dy, yaw+turn
    return x, y, yaw


def main():
    rng = Random(451)
    checked = 0
    for case in range(12):
        lower, upper = sorted((rng.uniform(-.02,.02), rng.uniform(-.02,.02)))
        estimate = ResidualBounds(.03)
        estimate.interval = lower, upper
        estimate.samples[0] = 0, 0., 0.
        moving, heading = 0., rng.uniform(-math.pi,math.pi)
        headings, steps = [heading], []
        for frame in range(1,101):
            distance = rng.uniform(-.022,.022)
            turn, applied = rng.uniform(-.035,.035), rng.uniform(-.02,.02)
            dt = .020 if abs(distance) > .004 else 0.
            dx,dy = arc(distance,heading,turn)
            moving += dt
            estimate.append(frame, moving, dt, applied, distance, dx, dy)
            steps.append((distance,turn,dt,applied))
            heading += turn
            headings.append(heading)
        for _ in range(12):
            start,end = rng.sample(range(101),2)
            path = steps[start:end] if start < end else [
                (-distance,-turn,-dt,applied) for distance,turn,dt,applied in reversed(steps[end:start])]
            x,y,yaw = follow(((d,t,0.,0.) for d,t,_,_ in path),headings[start],0.)
            position,yaw_bound = estimate.between(start,end)
            for i in range(11):
                rate = lower + (upper-lower)*i/10
                bx,by,byaw = follow(path,headings[start],rate)
                assert math.hypot(x-bx,y-by) <= position, (case,start,end,rate)
                assert abs(yaw-byaw) <= yaw_bound, (case,start,end,rate)
                checked += 1
    print(f'{checked} exact integrations covered, including reverse projection')


if __name__ == '__main__':
    main()
