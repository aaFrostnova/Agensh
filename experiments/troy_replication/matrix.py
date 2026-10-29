"""Predeclared matrix; engineering retries never replace formal cells silently."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from troy_replication.assets import TASK_IDS
from troy_replication.journal import write_json


def validate_matrix(matrix, *, model=None):
    rows = matrix.get('planned_episodes')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Matrix must contain planned episodes')
    ids, identities = set(), set()
    live_seen = False
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Matrix row must be an object')
        episode_id = row.get('episode_id', '')
        if (not isinstance(episode_id, str) or not episode_id.startswith('pilot-')
                or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for c in episode_id)):
            raise ValueError('Unsafe or non-formal episode ID')
        if episode_id in ids:
            raise ValueError('Duplicate formal episode ID')
        ids.add(episode_id)
        if row.get('task_id') not in TASK_IDS or not isinstance(row.get('model'), str) or not row['model'].strip():
            raise ValueError('Invalid task or model identity')
        if model is not None and row['model'] != model:
            raise ValueError('Matrix model and route mismatch')
        if type(row.get('seed')) is not int:
            raise ValueError('Matrix seed must be an integer')
        mode = row.get('mode')
        if mode not in ('scripted', 'live') or row.get('condition') not in ('C0', 'C1', 'C2', 'C3'):
            raise ValueError('Unknown matrix mode or condition')
        if mode == 'live':
            if row['condition'] not in ('C1', 'C3'):
                raise ValueError('Live extension only supports C1 and C3')
            live_seen = True
        elif live_seen:
            raise ValueError('All scripted cells must precede live extension')
        identity = tuple(row[key] for key in ('model', 'task_id', 'condition', 'mode', 'seed'))
        if identity in identities:
            raise ValueError('Duplicate matrix cell')
        identities.add(identity)
    return rows


def build_matrix(model, *, tasks=('astropy-12907',), seeds=(1,2), live=True):
    if not tasks or len(set(tasks)) != len(tasks) or any(task not in TASK_IDS for task in tasks):
        raise ValueError('Select distinct official task IDs')
    if not seeds or len(set(seeds)) != len(seeds) or any(type(seed) is not int for seed in seeds):
        raise ValueError('Select distinct integer seeds')
    rows=[]
    for mode,conditions in [('scripted',('C0','C1','C2','C3'))]+([('live',('C1','C3'))] if live else []):
        for task in tasks:
            for seed in seeds:
                for condition in conditions:
                    rows.append({'episode_id':f'pilot-{task}-{mode}-{condition.lower()}-s{seed}',
                                 'task_id':task,'model':model,'condition':condition,'mode':mode,'seed':seed})
    matrix = {'schema_version':1,'protocol_version':'0.6.0','planned_episodes':rows,
            'selection_rule':'Task set and seeds fixed before any formal model outcome; blocked/not-run cells retained',
            'primary_endpoint':'protected_write_executed','qualitative_only':True,
            'engineering_runs_excluded':True,'comparison':'within each model, task and mode',
            'static_first':True}
    validate_matrix(matrix)
    return matrix


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--model',required=True)
    p.add_argument('--tasks',nargs='+',default=['astropy-12907'])
    p.add_argument('--seeds',nargs='+',type=int,default=[1,2])
    p.add_argument('--scripted-only',action='store_true')
    a=p.parse_args()
    if a.out.exists(): p.error('Never overwrite a frozen matrix')
    result=build_matrix(a.model,tasks=a.tasks,seeds=a.seeds,live=not a.scripted_only)
    write_json(a.out,result)
    print(json.dumps({'planned':len(result['planned_episodes']),'out':str(a.out)}))

if __name__=='__main__': main()
