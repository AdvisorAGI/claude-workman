"""Pure motion-planner benchmark. Never posts input or observes a person's activity.

Run with the existing macOS helper interpreter and PYTHONPATH. These are
synthetic path scores, not real clicks, human performance or detector tests.
"""
import json
import math
import random
import resource
import statistics
import sys
import time


def main():
    from atmos_computer import motion
    results = {}
    for humanize in (False, True):
        durations, costs, points, errors, ratios = [], [], [], [], []
        hits = 0
        started_cpu = time.process_time()
        for i in range(1000):
            rng = random.Random(i)
            start = (rng.uniform(50, 300), rng.uniform(50, 300))
            end = (rng.uniform(400, 1100), rng.uniform(350, 650))
            t = time.perf_counter_ns()
            path = motion.plan_path(start, end, rng=rng, humanize=humanize)
            costs.append((time.perf_counter_ns() - t) / 1e6)
            durations.append(path[-1][0]); points.append(len(path))
            err = math.dist(path[-1][1:], end); errors.append(err)
            hits += abs(path[-1][1] - end[0]) <= 2 and abs(path[-1][2] - end[1]) <= 2
            xy = [start] + [(p[1], p[2]) for p in path]
            ratios.append(sum(math.dist(a, b) for a, b in zip(xy, xy[1:])) / math.dist(start, end))
            assert all(a[0] <= b[0] for a, b in zip(path, path[1:]))
        results["human_mode" if humanize else "direct_agent"] = {
            "paths": 1000, "synthetic_endpoint_score_percent": hits / 10,
            "max_endpoint_error_points": max(errors),
            "planner_ms_p50": statistics.median(costs),
            "planner_ms_p95": sorted(costs)[949],
            "planned_movement_seconds_p50": statistics.median(durations),
            "points_per_path_p50": statistics.median(points),
            "path_length_ratio_p50": statistics.median(ratios),
            "cpu_seconds_for_1000_plans": time.process_time() - started_cpu}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    results.update(peak_process_rss_bytes=rss if sys.platform == "darwin" else rss * 1024,
                   source=motion.__file__, kind="offline_synthetic_planner_only",
                   real_desktop_actions=0, human_baseline=None,
                   human_comparison="Not measured; a person must complete the same live fixture.",
                   detection_claim="None. This test cannot measure invisibility or bot detection.")
    print(json.dumps(results, indent=2))


if __name__ == "__main__": main()
