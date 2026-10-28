"""Deterministic validators for rules, deliverables and infeasibility reports."""
from __future__ import annotations

import jsonschema


QUADRATIC_FAMILY = "quadratic_mod97"
QUADRATIC_MODULUS = 97
QUADRATIC_DIMENSIONS = 6
QUADRATIC_COEFFICIENTS = 28


def _quadratic_value(coefficients, vector):
    """Evaluate the documented grammar, independently of generator linear algebra."""
    value = coefficients[0]
    value += sum(coefficients[1 + i] * vector[i] for i in range(6))
    offset = 7
    for i in range(6):
        for j in range(i, 6):
            value += coefficients[offset] * vector[i] * vector[j]
            offset += 1
    return value % 97


def run_cases(rules, cases):
    vector_cases = bool(cases) and isinstance(cases[0].get("input"), list)
    if vector_cases:
        valid = not rules_shape_errors(rules, family=QUADRATIC_FAMILY)
        passed = sum(1 for c in cases if valid and _quadratic_value(
            rules["coefficients"], c["input"]) == c["expected"])
    else:
        ok = isinstance(rules, dict)
        passed = sum(1 for c in cases if ok and rules.get(c["input"]) == c["expected"])
    return {"passed": passed, "failed": len(cases) - passed, "total": len(cases)}


def rules_shape_errors(obj, family=None):
    if family == QUADRATIC_FAMILY or isinstance(obj, dict) and obj.get("family") == QUADRATIC_FAMILY:
        if not isinstance(obj, dict) or set(obj) != {"family", "coefficients"}:
            return ["quadratic rules must contain exactly family and coefficients"]
        if obj.get("family") != QUADRATIC_FAMILY:
            return ["rules family must be quadratic_mod97"]
        coefficients = obj.get("coefficients")
        if not isinstance(coefficients, list) or len(coefficients) != QUADRATIC_COEFFICIENTS:
            return ["coefficients must contain exactly 28 integers"]
        if any(type(c) is not int or not 0 <= c < QUADRATIC_MODULUS for c in coefficients):
            return ["coefficients must be integers from 0 through 96 (booleans are not integers)"]
        return []
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
    if "infeasible" not in schema.get("definitions", {}):
        return ["this task has no infeasibility report format (exit: none)"]
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


def quadratic_system(cases):
    """Independent exact verifier/solver for the canonical finite-field system.

    Incremental reduced elimination differs from the generator's rank routine.
    It proves inconsistency by rank(A|b)>rank(A), rather than by search budgets.
    """
    basis = {}
    inconsistent = False
    for case in cases:
        x = case["input"]
        row = [1, *x]
        for left in range(6):
            for right in range(left, 6):
                row.append(x[left] * x[right])
        row.append(case["expected"])
        row = [entry % 97 for entry in row]
        for pivot in sorted(basis):
            multiplier = row[pivot]
            row = [(a - multiplier * b) % 97 for a, b in zip(row, basis[pivot])]
        pivot = next((index for index in range(28) if row[index]), None)
        if pivot is None:
            inconsistent = inconsistent or bool(row[-1])
            continue
        inverse = pow(row[pivot], 95, 97)
        row = [entry * inverse % 97 for entry in row]
        for old_pivot, old_row in list(basis.items()):
            multiplier = old_row[pivot]
            basis[old_pivot] = [(a - multiplier * b) % 97 for a, b in zip(old_row, row)]
        basis[pivot] = row
    return {
        "coefficient_rank": len(basis),
        "augmented_rank": len(basis) + int(inconsistent),
        "coefficients": [basis[i][-1] for i in range(28)] if len(basis) == 28 and not inconsistent else None,
    }
