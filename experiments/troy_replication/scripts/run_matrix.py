#!/usr/bin/env python3
"""Run a frozen, ordered matrix; preserve every result and stop on infrastructure failure."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from peer_influence.routes import load_routes,load_secret_env
from troy_replication.runner import run_episode,code_identity,require_allocation
from troy_replication.cohort import (build_freeze, freeze_or_verify, runtime_files_identity,
                                     validate_resume, CohortChanged)
from troy_replication.matrix import validate_matrix


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--root',required=True,type=Path)
    p.add_argument('--matrix',required=True,type=Path)
    p.add_argument('--route',required=True)
    p.add_argument('--routes',type=Path,default=Path(__file__).resolve().parents[1]/'configs/model_routes.yaml')
    p.add_argument('--secret-env',type=Path)
    p.add_argument('--max-seconds',type=int,default=1200)
    p.add_argument('--max-requests',type=int,default=32)
    a=p.parse_args(); require_allocation(a.root)
    if a.secret_env: os.environ.update(load_secret_env(a.secret_env))
    route=load_routes(a.routes)[a.route]
    matrix=json.loads(a.matrix.read_text()); rows=validate_matrix(matrix,model=route.model_id)
    repo=Path(__file__).resolve().parents[3]
    freeze_path=a.matrix.with_suffix('.freeze.json')
    previous=json.loads(freeze_path.read_text()) if freeze_path.exists() else None
    def current_freeze(expected):
        # Resolve environment-backed route fields again: unchanged YAML alone is insufficient.
        current_route=load_routes(a.routes)[a.route]
        return build_freeze(a.root,a.matrix,a.routes,current_route,code=code_identity(repo),
                            max_seconds=a.max_seconds,max_requests=a.max_requests,expected=expected,
                            runtime_files=runtime_files_identity(repo))
    freeze=freeze_or_verify(freeze_path,current_freeze(previous))
    def audit():
        path=a.root/'results/retrospective_audit.json'
        return json.loads(path.read_text()) if path.exists() else {}
    def valid_existing(row):
        return validate_resume(a.root/'results/episodes'/row['episode_id'],row,freeze,
                               retrospective_audit=audit())
    for row in rows:
        # Revalidate before every cell, including a resumed cell. No image-layer scans.
        freeze_or_verify(freeze_path,current_freeze(freeze))
        existing=a.root/'results/episodes'/row['episode_id']/'episode.json'
        if existing.exists():
            valid_existing(row)
            continue
        if (a.root/'results/episodes'/row['episode_id']).exists():
            raise RuntimeError('Incomplete existing cell; preserve and inspect before a new version')
        if row['mode']=='live':
            # Static conditions must all be technically valid before live extension.
            for static in (r for r in rows if r['mode']=='scripted'):
                check=a.root/'results/episodes'/static['episode_id']/'episode.json'
                if not check.exists():
                    raise RuntimeError('Static phase incomplete or invalid')
                valid_existing(static)
        result=run_episode(a.root,row['task_id'],row['condition'],row['mode'],row['seed'],route,
                           api_key=os.environ.get(route.api_key_env,''),episode_id=row['episode_id'],
                           max_seconds=a.max_seconds,max_requests=a.max_requests,
                           cohort_fingerprint=freeze['cohort_fingerprint'],
                           frozen_fixture_identity=freeze['fixtures'][row['task_id']])
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if not result['valid']: return 2
        valid_existing(row)
    return 0

if __name__=='__main__': raise SystemExit(main())
