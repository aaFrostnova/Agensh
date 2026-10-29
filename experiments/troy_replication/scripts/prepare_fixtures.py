#!/usr/bin/env python3
"""Rebuild real task snapshots without model-based source repairs."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from experiments.troy_replication.assets import load_task
from experiments.troy_replication.fixtures import FixtureStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--tasks', nargs='+', default=['astropy-12907', 'django-10554'])
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--no-bwrap-check', action='store_true')
    args = parser.parse_args()
    store = FixtureStore(args.root)
    status = 0
    for task_id in args.tasks:
        record = store.prepare(load_task(task_id), timeout=args.timeout, verify_bwrap=not args.no_bwrap_check)
        print(json.dumps({'task_id': record['task_id'], 'status': record['status'], 'blockers': record['blockers']}), flush=True)
        status |= record['status'] != 'validated'
    return int(status)

if __name__ == '__main__':
    raise SystemExit(main())
