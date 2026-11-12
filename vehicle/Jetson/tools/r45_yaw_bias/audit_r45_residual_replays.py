"""Score published positions and headings against the untouched replay trajectory."""

import argparse
from itertools import groupby
import json
import math
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('results', type=Path)
    options = parser.parse_args()
    cases = ('positive', 'negative', 'zero', 'heldout-p2')
    for case in cases:
        name = f'r45-yaw-residual-quantized-{case}-metrics.json'
        result = json.loads((options.results/name).read_bytes())
        rows = result['rows']
        assert result['samples']['gateway_rejected'] == 0
        assert result['interval_contradiction_at'] is None
        assert all(row['rate_interval'][0] <= result['rate'] <= row['rate_interval'][1] for row in rows)
        assert result['cones']['B']['first_s'] == 17.8
        missing = [(group[0]['at'], group[-1]['at'], len(group))
                   for absent, sequence in groupby(rows, key=lambda row: row['mapped'] is None)
                   if absent for group in [list(sequence)]]
        position_outside = []
        yaw_outside = []
        for row in rows:
            if row['mapped'] is None:
                continue
            error = math.dist(row['mapped'][:2], row['truth'][:2])
            yaw = abs(math.remainder(row['mapped'][2] - row['truth'][2], 2*math.pi))
            if not error <= row['bound'] <= .05:
                position_outside.append((row['at'], row['status'], error, row['bound']))
            if yaw > row['yaw_bound']:
                yaw_outside.append((row['at'], row['status'], yaw, row['yaw_bound']))
        first_b = next(row for row in rows if row['at']==17.8)
        print(case, result['pose_frames'], 'missing', missing, 'B at', first_b['at'],
              'position bound cm', round(first_b['bound']*100, 3),
              'position outside', position_outside, 'yaw outside', yaw_outside)
        assert not position_outside and not yaw_outside, case


if __name__ == '__main__':
    main()
