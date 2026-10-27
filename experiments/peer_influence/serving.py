"""Provider/serving metadata recorded in every manifest (plan 8.2 record_fields). No credentials."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

QWEN_SERVE_PY = "/scratch/workspace/mingzhel_umass_edu-ablator/envs/cnr-qwen38/bin/python"
QWEN_WEIGHTS = ("/scratch/workspace/mingzhel_umass_edu-ablator/models/Qwen3.8-27B/"
                "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0")


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def serving_info(route, project_root):
    if route.deployment == "managed":
        m = re.match(r"https://bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com", route.upstream_url)
        return {"aws_region": m.group(1) if m else None, "inference_profile": route.model_id,
                "service_tier": route.extras.get("service_tier"), "provider": route.provider}
    info = {"dtype": "bfloat16", "quantization": None, "tensor_parallel_size": 1, "gpu_count": 1,
            "provider": route.provider, "weight_revision": Path(os.environ.get("QWEN_WEIGHTS", QWEN_WEIGHTS)).name}
    if project_root is not None:
        serve_py = os.environ.get("QWEN_SERVE_PY", QWEN_SERVE_PY)
        info["vllm_version"] = _run([serve_py, "-c", "import vllm; print(vllm.__version__)"])
        gpu = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        info["gpu_model"] = gpu.splitlines()[0] if gpu else None
        info["host"] = os.uname().nodename
    return info
