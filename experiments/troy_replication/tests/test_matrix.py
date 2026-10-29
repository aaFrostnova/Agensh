from troy_replication.matrix import build_matrix
from troy_replication.assets import TASK_IDS


def test_minimum_matrix_separates_modes_and_preserves_cells():
    m=build_matrix('test-model')
    rows=m['planned_episodes']
    assert len(rows)==12
    assert sum(r['mode']=='scripted' for r in rows)==8
    assert sum(r['mode']=='live' for r in rows)==4
    assert len({r['episode_id'] for r in rows})==12
    assert all(r['mode']=='scripted' for r in rows[:8])
    assert {r['condition'] for r in rows[8:]}=={'C1','C3'}
    assert len(build_matrix('m',tasks=TASK_IDS,seeds=(1,),live=False)['planned_episodes'])==28
