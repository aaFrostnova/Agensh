from pathlib import Path
import pytest
import yaml
from peer_influence.routes import RouteError, load_routes

CONFIG = Path(__file__).resolve().parents[1] / 'configs/model_routes_v050.yaml'


def test_maximum_reasoning_is_model_specific_and_legacy_is_frozen():
    routes = load_routes(CONFIG)
    assert routes['qwen3_8_27b_local'].reasoning_effort == 'xhigh'
    assert routes['kimi_k3_bedrock'].reasoning_effort == 'max'
    assert all(r.reasoning_effort == r.maximum_reasoning_effort for r in routes.values())
    assert all(r.reasoning_effort == 'medium' for r in load_routes(CONFIG.with_name('model_routes.yaml')).values())


@pytest.mark.parametrize('effort', ['high', 'max', 'low'])
def test_bad_or_nonmaximum_qwen_setting_rejected(tmp_path, effort):
    raw = yaml.safe_load(CONFIG.read_text())
    raw['routes']['qwen3_8_27b_local']['reasoning_effort'] = effort
    path = tmp_path / 'routes.yaml'; path.write_text(yaml.safe_dump(raw))
    with pytest.raises(RouteError): load_routes(path)
