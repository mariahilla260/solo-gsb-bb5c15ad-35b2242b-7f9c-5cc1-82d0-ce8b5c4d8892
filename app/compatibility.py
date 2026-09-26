"""Client-compatibility checking between a baseline and a candidate schema.

An existing GraphQL client keeps working against the candidate schema when:

* every field that existed on the baseline still exists (per object /
  interface type) with the same named type and the same list nesting;
* return types only ever *strengthen* non-null constraints
  (``String`` -> ``String!`` is safe, the reverse is not);
* argument types only ever *relax* non-null constraints
  (``Int!`` -> ``Int`` is safe, the reverse is not);
* newly added arguments are optional (nullable or declared with a
  default value).

The checker reports removed fields, newly added required arguments and every
type-shape violation together with its type path and a human-readable reason.
"""

from __future__ import annotations

import time
from typing import Any

from graphql import (
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    build_schema,
)
from graphql.error import GraphQLError

FIELD_CONTAINERS = (GraphQLObjectType, GraphQLInterfaceType)

FIELD_REMOVED = "FIELD_REMOVED"
RETURN_NAMED_TYPE_CHANGED = "RETURN_NAMED_TYPE_CHANGED"
RETURN_LIST_DEPTH_CHANGED = "RETURN_LIST_DEPTH_CHANGED"
RETURN_NULLABILITY_WEAKENED = "RETURN_NULLABILITY_WEAKENED"
ARGUMENT_NAMED_TYPE_CHANGED = "ARGUMENT_NAMED_TYPE_CHANGED"
ARGUMENT_LIST_DEPTH_CHANGED = "ARGUMENT_LIST_DEPTH_CHANGED"
ARGUMENT_NULLABILITY_STRENGTHENED = "ARGUMENT_NULLABILITY_STRENGTHENED"
REQUIRED_ARGUMENT_ADDED = "REQUIRED_ARGUMENT_ADDED"

_TYPE_VIOLATION_CODES = frozenset(
    {
        RETURN_NAMED_TYPE_CHANGED,
        RETURN_LIST_DEPTH_CHANGED,
        RETURN_NULLABILITY_WEAKENED,
        ARGUMENT_NAMED_TYPE_CHANGED,
        ARGUMENT_LIST_DEPTH_CHANGED,
        ARGUMENT_NULLABILITY_STRENGTHENED,
    }
)


def check_schema_compatibility(
    *, baseline_sdl: str, candidate_sdl: str
) -> dict[str, Any]:
    """Compare ``baseline_sdl`` against ``candidate_sdl``.

    Never raises: SDL problems are reported in the returned ``errors`` list
    with an ``invalid`` status.
    """
    started = time.perf_counter()
    errors: list[dict[str, Any]] = []
    baseline = _try_build_schema(baseline_sdl, "baseline", errors)
    candidate = _try_build_schema(candidate_sdl, "candidate", errors)

    if errors:
        result: dict[str, Any] = {
            "status": "invalid",
            "compatible": False,
            "summary": None,
            "violations": [],
            "errors": errors,
        }
    else:
        violations = _diff_schemas(baseline, candidate)
        compatible = not violations
        result = {
            "status": "ok" if compatible else "incompatible",
            "compatible": compatible,
            "summary": _summarize(baseline, violations),
            "violations": violations,
            "errors": [],
        }
    result["meta"] = {
        "analysis_ms": round((time.perf_counter() - started) * 1000.0, 3),
    }
    return result


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #


def _try_build_schema(sdl: str, role: str, errors: list[dict[str, Any]]):
    """Build a schema, appending a structured error and returning None on failure."""
    try:
        return build_schema(sdl)
    except GraphQLError as exc:
        errors.append(
            {
                "code": f"INVALID_{role.upper()}_SDL",
                "message": exc.message,
                "details": {
                    "schema": role,
                    "locations": [
                        {"line": loc.line, "column": loc.column}
                        for loc in exc.locations or []
                    ],
                },
            }
        )
        return None


def _type_shape(type_) -> tuple[str, tuple[bool, ...]]:
    """Reduce a type to ``(named type, nullability levels)``.

    ``levels`` runs from the named type (position 0) outwards to the
    outermost list wrapper; ``True`` means nullable at that level. The list
    nesting depth is therefore ``len(levels) - 1``. ``String!`` yields
    ``("String", (False,))`` and ``[User!]!`` yields ``("User", (False, False))``.
    """
    levels: list[bool] = []

    def recurse(current, nullable: bool) -> str:
        if isinstance(current, GraphQLNonNull):
            return recurse(current.of_type, False)
        if isinstance(current, GraphQLList):
            named = recurse(current.of_type, True)
            levels.append(nullable)
            return named
        levels.append(nullable)
        return current.name

    named = recurse(type_, True)
    return named, tuple(levels)


def _shape_diff(
    baseline_shape: tuple[str, tuple[bool, ...]],
    candidate_shape: tuple[str, tuple[bool, ...]],
) -> tuple[bool, bool, list[int], list[int]]:
    """Compare two shapes.

    Returns ``(named_changed, depth_changed, null_added, null_removed)`` with
    positions counted from the named type (0):

    * ``null_added``: nullable -> non-null between baseline and candidate
      (safe on return types, unsafe on arguments);
    * ``null_removed``: non-null -> nullable (unsafe on return types, safe
      on arguments).

    Position comparison only happens when the list depth is unchanged.
    """
    baseline_named, baseline_levels = baseline_shape
    candidate_named, candidate_levels = candidate_shape
    named_changed = baseline_named != candidate_named
    depth_changed = len(baseline_levels) != len(candidate_levels)
    null_added: list[int] = []
    null_removed: list[int] = []
    if not depth_changed:
        for position, (before, after) in enumerate(
            zip(baseline_levels, candidate_levels)
        ):
            if before and not after:
                null_added.append(position)
            elif after and not before:
                null_removed.append(position)
    return named_changed, depth_changed, null_added, null_removed


def _is_required_argument(argument) -> bool:
    """Non-null argument without a default value in the SDL."""
    if not isinstance(argument.type, GraphQLNonNull):
        return False
    ast_node = getattr(argument, "ast_node", None)
    return ast_node is None or ast_node.default_value is None


def _violation(
    code: str,
    path: str,
    reason: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"code": code, "path": path, "reason": reason, "details": details}


# --------------------------------------------------------------------------- #
# Schema diff
# --------------------------------------------------------------------------- #


def _diff_schemas(baseline, candidate) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for type_name in sorted(baseline.type_map):
        if type_name.startswith("__"):
            continue
        baseline_type = baseline.type_map[type_name]
        if not isinstance(baseline_type, FIELD_CONTAINERS):
            continue
        candidate_type = candidate.type_map.get(type_name)
        candidate_fields = (
            candidate_type.fields
            if isinstance(candidate_type, FIELD_CONTAINERS)
            else {}
        )
        for field_name in sorted(baseline_type.fields):
            baseline_field = baseline_type.fields[field_name]
            path = f"{type_name}.{field_name}"
            candidate_field = candidate_fields.get(field_name)
            if candidate_field is None:
                violations.append(
                    _violation(
                        FIELD_REMOVED,
                        path,
                        f"Field '{path}' exists in the baseline schema but is "
                        f"missing from the candidate; clients selecting it "
                        f"would break.",
                    )
                )
                continue
            violations.extend(
                _compare_field(path, baseline_field, candidate_field)
            )
    return violations


def _compare_field(path, baseline_field, candidate_field) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    baseline_type = str(baseline_field.type)
    candidate_type = str(candidate_field.type)
    named_changed, depth_changed, null_added, null_removed = _shape_diff(
        _type_shape(baseline_field.type), _type_shape(candidate_field.type)
    )
    if named_changed:
        violations.append(
            _violation(
                RETURN_NAMED_TYPE_CHANGED,
                path,
                f"Return type of '{path}' changed its named type from "
                f"'{baseline_type}' to '{candidate_type}'; the named type "
                f"must stay the same.",
                _type_details(baseline_type, candidate_type),
            )
        )
    if depth_changed:
        violations.append(
            _violation(
                RETURN_LIST_DEPTH_CHANGED,
                path,
                f"Return type of '{path}' changed list nesting from "
                f"'{baseline_type}' to '{candidate_type}'; list levels must "
                f"stay the same.",
                _type_details(baseline_type, candidate_type),
            )
        )
    if null_removed:
        violations.append(
            _violation(
                RETURN_NULLABILITY_WEAKENED,
                path,
                f"Return type of '{path}' weakened a non-null constraint from "
                f"'{baseline_type}' to '{candidate_type}'; return types may "
                f"only strengthen non-null constraints.",
                _type_details(baseline_type, candidate_type, null_removed),
            )
        )

    violations.extend(_compare_arguments(path, baseline_field, candidate_field))
    return violations


def _compare_arguments(
    path, baseline_field, candidate_field
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for arg_name in sorted(baseline_field.args):
        baseline_arg = baseline_field.args[arg_name]
        candidate_arg = candidate_field.args.get(arg_name)
        if candidate_arg is None:
            # Not part of the reported compatibility contract: only removed
            # fields, added required arguments and type-shape changes are.
            continue
        arg_path = f"{path}({arg_name})"
        baseline_type = str(baseline_arg.type)
        candidate_type = str(candidate_arg.type)
        named_changed, depth_changed, null_added, _ = _shape_diff(
            _type_shape(baseline_arg.type), _type_shape(candidate_arg.type)
        )
        if named_changed:
            violations.append(
                _violation(
                    ARGUMENT_NAMED_TYPE_CHANGED,
                    arg_path,
                    f"Argument '{arg_path}' changed its named type from "
                    f"'{baseline_type}' to '{candidate_type}'; the named type "
                    f"must stay the same.",
                    _type_details(baseline_type, candidate_type),
                )
            )
        if depth_changed:
            violations.append(
                _violation(
                    ARGUMENT_LIST_DEPTH_CHANGED,
                    arg_path,
                    f"Argument '{arg_path}' changed list nesting from "
                    f"'{baseline_type}' to '{candidate_type}'; list levels "
                    f"must stay the same.",
                    _type_details(baseline_type, candidate_type),
                )
            )
        if null_added:
            violations.append(
                _violation(
                    ARGUMENT_NULLABILITY_STRENGTHENED,
                    arg_path,
                    f"Argument '{arg_path}' strengthened a non-null constraint "
                    f"from '{baseline_type}' to '{candidate_type}'; argument "
                    f"types may only relax non-null constraints.",
                    _type_details(baseline_type, candidate_type, null_added),
                )
            )
    for arg_name in sorted(candidate_field.args):
        if arg_name in baseline_field.args:
            continue
        candidate_arg = candidate_field.args[arg_name]
        if _is_required_argument(candidate_arg):
            arg_path = f"{path}({arg_name})"
            violations.append(
                _violation(
                    REQUIRED_ARGUMENT_ADDED,
                    arg_path,
                    f"Argument '{arg_path}' was added as a required argument "
                    f"(type '{candidate_arg.type}' with no default value); "
                    f"existing client calls would fail validation.",
                    {"argument_type": str(candidate_arg.type)},
                )
            )
    return violations


def _type_details(
    baseline_type: str,
    candidate_type: str,
    positions: list[int] | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "baseline_type": baseline_type,
        "candidate_type": candidate_type,
    }
    if positions:
        # Positions count from the named type (0) outwards.
        details["positions"] = positions
    return details


def _summarize(baseline, violations: list[dict[str, Any]]) -> dict[str, Any]:
    checked_types = 0
    checked_fields = 0
    for type_name, type_def in baseline.type_map.items():
        if type_name.startswith("__") or not isinstance(
            type_def, FIELD_CONTAINERS
        ):
            continue
        checked_types += 1
        checked_fields += len(type_def.fields)
    return {
        "compatible": not violations,
        "violation_count": len(violations),
        "removed_field_count": sum(
            1 for v in violations if v["code"] == FIELD_REMOVED
        ),
        "required_argument_count": sum(
            1 for v in violations if v["code"] == REQUIRED_ARGUMENT_ADDED
        ),
        "type_violation_count": sum(
            1 for v in violations if v["code"] in _TYPE_VIOLATION_CODES
        ),
        "checked_type_count": checked_types,
        "checked_field_count": checked_fields,
    }
