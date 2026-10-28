"""Condition matrix (plan 13): every cell the same number of times, order randomized per cohort."""
from __future__ import annotations

import random

DIFFICULTIES = ("solvable", "impossible")
PEERS = ("solo", "benign", "malicious")
CONDITIONS = tuple((d, p) for d in DIFFICULTIES for p in PEERS)


def plan_matrix(task_pairs, repeats, route, experiment_id, seed, family="mapping"):
    """24 episodes for (2 pairs, 2 repeats); 180 for (10, 3).

    The shuffle is seeded by experiment, route and seed, so a replication cohort on
    another model gets its own independent order.
    """
    if family not in ("mapping", "quadratic_mod97"):
        raise ValueError(f"unknown task family {family!r}")
    prefix = "quadratic" if family == "quadratic_mod97" else "mapping"
    cells = [
        {"pair_id": f"{prefix}_{i:03d}", "difficulty": d, "peer_condition": p, "seed": r}
        for i in range(task_pairs) for d, p in CONDITIONS for r in range(1, repeats + 1)
    ]
    random.Random(f"{experiment_id}:{route}:{seed}").shuffle(cells)
    for n, cell in enumerate(cells, 1):
        cell.update(
            episode_id=f"ep_{n:06d}", task_id=f"{cell['pair_id']}_{cell['difficulty']}",
            experiment_id=experiment_id, model_route=route, run_attempt=1,
        )
    return cells


def treatment_type(peer_condition):
    """Experiment condition -> controller treatment: `solo` is the no-message arm."""
    if peer_condition not in PEERS:
        raise ValueError(f"unknown peer condition {peer_condition!r}")
    return "none" if peer_condition == "solo" else peer_condition


def filter_plan(plan, peers=None, limit=None, skip=()):
    """Subset of a plan in its original run order; an explicit, visible way to run part of a cohort."""
    if peers is not None:
        unknown = set(peers) - set(PEERS)
        if unknown:
            raise ValueError(f"unknown peer conditions {sorted(unknown)}")
    chosen = [s for s in plan if (peers is None or s["peer_condition"] in peers) and s["episode_id"] not in set(skip)]
    return chosen if limit is None else chosen[:limit]
