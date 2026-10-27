"""Condition matrix (plan 13): every cell the same number of times, order randomized per cohort."""
from __future__ import annotations

import random

DIFFICULTIES = ("solvable", "impossible")
PEERS = ("solo", "benign", "malicious")
CONDITIONS = tuple((d, p) for d in DIFFICULTIES for p in PEERS)


def plan_matrix(task_pairs, repeats, route, experiment_id, seed):
    """24 episodes for (2 pairs, 2 repeats); 180 for (10, 3).

    The shuffle is seeded by experiment, route and seed, so a replication cohort on
    another model gets its own independent order.
    """
    cells = [
        {"pair_id": f"mapping_{i:03d}", "difficulty": d, "peer_condition": p, "seed": r}
        for i in range(task_pairs) for d, p in CONDITIONS for r in range(1, repeats + 1)
    ]
    random.Random(f"{experiment_id}:{route}:{seed}").shuffle(cells)
    for n, cell in enumerate(cells, 1):
        cell.update(
            episode_id=f"ep_{n:06d}", task_id=f"{cell['pair_id']}_{cell['difficulty']}",
            experiment_id=experiment_id, model_route=route, run_attempt=1,
        )
    return cells
