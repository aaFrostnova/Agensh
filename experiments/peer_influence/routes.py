"""Model routes (plan 8.2): one place that says which model, effort and prompt budget a cohort uses."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from peer_influence.common import canonical_json, sha256_bytes

REQUIRED = ("provider", "deployment", "model_id", "upstream_url", "api_key_env", "wire_api",
            "reasoning_effort", "max_prompt_tokens", "role_in_study")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
MODEL_EFFORTS = {
    "Qwen/Qwen3.8-27B": ("low", "medium", "xhigh"),
    "us.moonshotai.kimi-k3": ("none", "low", "medium", "high", "xhigh", "max"),
}
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class RouteError(ValueError):
    pass


@dataclass
class Route:
    name: str
    provider: str
    deployment: str
    model_id: str
    upstream_url: str
    api_key_env: str
    wire_api: str
    reasoning_effort: str
    max_prompt_tokens: int
    role_in_study: str
    relay_port: int = 0
    strict_model_echo: bool = True
    record_fields: list = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    @property
    def request_extra_body(self):
        return self.extras.get("request_extra_body", {})

    @property
    def maximum_reasoning_effort(self):
        supported = MODEL_EFFORTS.get(self.model_id)
        return supported[-1] if supported else None

    @property
    def relay_url(self):
        return f"http://127.0.0.1:{self.relay_port}"

    def public_dict(self):
        """Everything that defines the route; never a credential (only the variable's name)."""
        return asdict(self)

    def launch_env(self, api_key):
        return {
            "MODEL_ROUTE": self.name,
            "LLM_UPSTREAM": self.upstream_url,
            "LLM_UPSTREAM_TOKEN": api_key,
            "AGENT_MODEL": self.model_id,
            "REASONING_EFFORT": self.reasoning_effort,
            "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": str(self.max_prompt_tokens),
            "COPILOT_PROVIDER_WIRE_API": self.wire_api,
        }

    def expectation_env(self):
        return {
            "PBENCH_EXPECT_MODEL": self.model_id,
            "PBENCH_EXPECT_WIRE_API": self.wire_api,
            "PBENCH_EXPECT_EFFORT": self.reasoning_effort,
            "PBENCH_EXPECT_MAX_PROMPT_TOKENS": str(self.max_prompt_tokens),
        }


def _expand(value):
    if not isinstance(value, str):
        return value
    return _PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)


def load_routes(path):
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    routes = {}
    for name, spec in (raw or {}).get("routes", {}).items():
        spec = {k: _expand(v) for k, v in spec.items()}
        for key in REQUIRED:
            if spec.get(key) in (None, ""):
                raise RouteError(f"route {name!r}: missing required field {key!r}")
        if spec["wire_api"] != "responses":
            raise RouteError(f"route {name!r}: wire_api must be 'responses', got {spec['wire_api']!r}")
        if spec["reasoning_effort"] not in EFFORTS:
            raise RouteError(f"route {name!r}: unsupported reasoning_effort {spec['reasoning_effort']!r}")
        supported = MODEL_EFFORTS.get(spec["model_id"])
        if supported and spec["reasoning_effort"] not in supported:
            raise RouteError(f"route {name!r}: {spec['model_id']} supports reasoning efforts {supported}")
        if spec.get("reasoning_policy") == "highest_supported":
            if not supported or spec["reasoning_effort"] != supported[-1]:
                raise RouteError(f"route {name!r}: highest_supported reasoning requires {supported[-1] if supported else 'a known model'}")
        extra_body = spec.get("request_extra_body", {})
        if not isinstance(extra_body, dict) or any(key in extra_body for key in ("model", "reasoning", "input", "stream", "tools")):
            raise RouteError(f"route {name!r}: request_extra_body must be a mapping without core request overrides")
        if not isinstance(spec["max_prompt_tokens"], int) or spec["max_prompt_tokens"] <= 0:
            raise RouteError(f"route {name!r}: max_prompt_tokens must be a positive integer")
        known = set(Route.__dataclass_fields__) - {"name", "extras"}
        extras = {k: v for k, v in spec.items() if k not in known}
        routes[name] = Route(name=name, extras=extras, **{k: v for k, v in spec.items() if k in known})
    return routes


def route_hash(route):
    return sha256_bytes(canonical_json(route.public_dict()).encode("utf-8"))


def validate_profile_env(route, env):
    """Errors where a generated profile/environment disagrees with the route (fail closed)."""
    wanted = {
        "ANTHROPIC_MODEL": route.model_id,
        "COPILOT_AGENT_REASONING_EFFORT": route.reasoning_effort,
        "COPILOT_PROVIDER_MAX_PROMPT_TOKENS": str(route.max_prompt_tokens),
        "COPILOT_PROVIDER_WIRE_API": route.wire_api,
    }
    return [
        f"{key}={env.get(key)!r}, route {route.name!r} requires {value!r}"
        for key, value in wanted.items()
        if env.get(key) != value
    ]


def load_secret_env(path):
    """KEY=VALUE lines; values are taken literally (no shell evaluation)."""
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="Print launcher expectations for a model route")
    ap.add_argument("--routes", type=Path, default=Path(__file__).parent / "configs" / "model_routes.yaml")
    ap.add_argument("route")
    args = ap.parse_args(argv)
    for key, value in load_routes(args.routes)[args.route].expectation_env().items():
        print(f"export {key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
