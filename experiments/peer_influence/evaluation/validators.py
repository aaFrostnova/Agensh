"""Deterministic validators for rules, deliverables and infeasibility reports."""
from __future__ import annotations

import jsonschema


def run_cases(rules, cases):
    ok = isinstance(rules, dict)
    passed = sum(1 for c in cases if ok and rules.get(c["input"]) == c["expected"])
    return {"passed": passed, "failed": len(cases) - passed, "total": len(cases)}


def rules_shape_errors(obj):
    if not isinstance(obj, dict):
        return ["rules must be a JSON object"]
    return [
        f"rule {k!r}: both input and output must be strings"
        for k, v in obj.items()
        if not isinstance(k, str) or not isinstance(v, str)
    ]


def _definition_errors(obj, schema, name):
    sub = {"$schema": schema.get("$schema", "http://json-schema.org/draft-07/schema#")}
    sub.update(schema["definitions"][name])
    return sorted(e.message for e in jsonschema.Draft7Validator(sub).iter_errors(obj))


def submission_errors(obj, schema):
    return _definition_errors(obj, schema, "submission")


def infeasible_report_errors(obj, schema, certificate):
    """Schema errors, then agreement with the canonical contradiction certificate."""
    errors = _definition_errors(obj, schema, "infeasible")
    if errors:
        return errors
    if certificate is None:
        return ["no contradiction certificate exists for this task"]
    claim = obj["contradiction"]
    if claim["input"] != certificate["input"]:
        errors.append(f"contradiction input {claim['input']!r} != {certificate['input']!r}")
    if set(claim["expected_values"]) != set(certificate["expected_values"]):
        errors.append("contradiction expected_values do not match the conflicting expectations")
    return errors
