"""Backward-compatibility checks between two GraphQL SDL schemas.

Given a *baseline* schema (what existing clients were written against)
and a *candidate* schema (a proposed new version), decide whether those
clients can keep working unchanged.

Rules for a field present in both schemas (same parent type + name):

* The **named return type** must stay the same (``User`` cannot become
  ``Account``).
* The **list nesting** must stay the same at every level
  (``[T]`` ↮ ``T``, ``[[T]]`` ↮ ``[T]``).
* The return type may only be **strengthened** with non-null
  constraints (``String`` → ``String!``, ``[String]`` → ``[String!]!``).
  Dropping a ``!`` is breaking: clients would now have to expect null
  where the old schema promised a value.
* For field **arguments** the non-null direction is reversed: the
  candidate may only *loosen* (``Int!`` → ``Int``), since every request
  valid under the stricter baseline stays valid. Adding a ``!``
  (``Int`` → ``Int!``) can reject previously-valid requests.
* A newly added argument is safe when it is nullable or has a default
  value. A new argument that is non-null **and** has no default must be
  supplied by every existing call and is therefore breaking.
* Removing a field is breaking: documents selecting it fail validation
  against the candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from graphql import (
    GraphQLError,
    GraphQLInterfaceType,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    Undefined,
)

# Reuse the analyzer's schema builder so an optional @cost directive does
# not have to be re-declared in either SDL.
from .analyzer import _build_schema, _locations

FIELD_CONTAINERS = (GraphQLObjectType, GraphQLInterfaceType)


@dataclass(frozen=True)
class _TypeShape:
    """Normalized view of a GraphQL type wrapper stack.

    ``list_nonnull`` records, outer-to-inner, whether each list wrapper
    itself is non-null; ``named_nonnull`` records whether the innermost
    named type is non-null. For ``[User!]!`` this is
    ``(True,)`` + ``True``; for ``[User!]`` it is ``(False,)`` + ``True``.
    """

    named_name: str
    list_nonnull: tuple[bool, ...]
    named_nonnull: bool


def _type_shape(type_) -> _TypeShape:
    list_nonnull: list[bool] = []

    def visit(current, nonnull: bool) -> _TypeShape:
        if isinstance(current, GraphQLNonNull):
            return visit(current.of_type, True)
        if isinstance(current, GraphQLList):
            list_nonnull.append(nonnull)
            return visit(current.of_type, False)
        return _TypeShape(current.name, tuple(list_nonnull), nonnull)

    return visit(type_, False)


def _list_level_label(index: int, depth: int) -> str:
    if depth == 1:
        return "the list"
    if index == 1:
        return "the outer list"
    if index == depth:
        return "the inner list"
    return f"list level {index}"


def _compare_output_type(base_type, candidate_type) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when an output-type change is breaking."""
    base = _type_shape(base_type)
    candidate = _type_shape(candidate_type)
    base_str, candidate_str = str(base_type), str(candidate_type)

    if base.named_name != candidate.named_name:
        return (
            "FIELD_NAMED_TYPE_CHANGED",
            f"Named return type changed from '{base.named_name}' to "
            f"'{candidate.named_name}' ('{base_str}' -> '{candidate_str}').",
        )
    if len(base.list_nonnull) != len(candidate.list_nonnull):
        return (
            "FIELD_LIST_DEPTH_CHANGED",
            f"List nesting changed from '{base_str}' to '{candidate_str}': "
            f"{len(base.list_nonnull)} list level(s) to "
            f"{len(candidate.list_nonnull)}.",
        )
    # Only adding '!' (strengthening) is allowed for output types.
    for level, (old, new) in enumerate(
        zip(base.list_nonnull, candidate.list_nonnull), start=1
    ):
        if old and not new:
            where = _list_level_label(level, len(base.list_nonnull))
            return (
                "FIELD_NULLABILITY_WEAKENED",
                f"Non-null constraint on {where} was removed "
                f"('{base_str}' -> '{candidate_str}'); clients may now "
                "receive null where the baseline promised a value.",
            )
    if base.named_nonnull and not candidate.named_nonnull:
        return (
            "FIELD_NULLABILITY_WEAKENED",
            f"Non-null named type '{base.named_name}!' became nullable "
            f"('{base_str}' -> '{candidate_str}'); clients may now receive "
            "null for this field.",
        )
    return None


def _compare_argument_type(base_type, candidate_type) -> tuple[str, str] | None:
    """Return ``(code, reason)`` when an argument-type change is breaking."""
    base = _type_shape(base_type)
    candidate = _type_shape(candidate_type)
    base_str, candidate_str = str(base_type), str(candidate_type)

    if base.named_name != candidate.named_name:
        return (
            "ARGUMENT_NAMED_TYPE_CHANGED",
            f"Argument named type changed from '{base.named_name}' to "
            f"'{candidate.named_name}' ('{base_str}' -> '{candidate_str}').",
        )
    if len(base.list_nonnull) != len(candidate.list_nonnull):
        return (
            "ARGUMENT_LIST_DEPTH_CHANGED",
            f"Argument list nesting changed from '{base_str}' to "
            f"'{candidate_str}': {len(base.list_nonnull)} list level(s) "
            f"to {len(candidate.list_nonnull)}.",
        )
    # Arguments may only LOOSEN: adding '!' (strengthening) is breaking.
    for level, (old, new) in enumerate(
        zip(base.list_nonnull, candidate.list_nonnull), start=1
    ):
        if not old and new:
            where = _list_level_label(level, len(base.list_nonnull))
            return (
                "ARGUMENT_NULLABILITY_STRENGTHENED",
                f"Non-null constraint on {where} was added "
                f"('{base_str}' -> '{candidate_str}'); existing requests "
                "that omit this value may be rejected.",
            )
    if not base.named_nonnull and candidate.named_nonnull:
        return (
            "ARGUMENT_NULLABILITY_STRENGTHENED",
            f"Argument type '{base_str}' became non-null "
            f"('{base_str}' -> '{candidate_str}'); existing requests that "
            "omit this value may be rejected.",
        )
    return None


def _is_required_argument(argument) -> bool:
    """An argument is required when non-null and it has no default value."""
    return (
        isinstance(argument.type, GraphQLNonNull)
        and argument.default_value is Undefined
    )


def _schema_error(exc: GraphQLError, side: str) -> dict[str, Any]:
    return {
        "code": "GRAPHQL_ERROR",
        "message": exc.message,
        "path": None,
        "details": {"side": side, "locations": _locations(exc)},
    }


def _empty_report() -> dict[str, Any]:
    return {
        "compatible": False,
        "removed_fields": [],
        "added_required_arguments": [],
        "violations": [],
        "errors": [],
    }


def check_schema_compatibility(
    *, baseline_sdl: str, candidate_sdl: str
) -> dict[str, Any]:
    """Compare two SDL documents from the point of view of existing clients.

    Never raises: malformed SDL is reported in ``errors`` with
    ``compatible=False``.
    """
    try:
        baseline = _build_schema(baseline_sdl)
    except GraphQLError as exc:
        report = _empty_report()
        report["errors"].append(_schema_error(exc, "baseline"))
        return report
    try:
        candidate = _build_schema(candidate_sdl)
    except GraphQLError as exc:
        report = _empty_report()
        report["errors"].append(_schema_error(exc, "candidate"))
        return report

    report = _empty_report()

    container_names = sorted(
        name
        for name, type_ in baseline.type_map.items()
        if not name.startswith("__") and isinstance(type_, FIELD_CONTAINERS)
    )

    for type_name in container_names:
        base_type = baseline.type_map[type_name]
        candidate_type = candidate.get_type(type_name)

        # The type disappeared or changed kind: every field is gone.
        if not isinstance(candidate_type, FIELD_CONTAINERS):
            for field_name in sorted(base_type.fields):
                field_def = base_type.fields[field_name]
                report["removed_fields"].append(
                    {
                        "path": f"{type_name}.{field_name}",
                        "type": str(field_def.type),
                    }
                )
            continue

        for field_name in sorted(base_type.fields):
            base_field = base_type.fields[field_name]
            candidate_field = candidate_type.fields.get(field_name)
            field_path = f"{type_name}.{field_name}"

            if candidate_field is None:
                report["removed_fields"].append(
                    {"path": field_path, "type": str(base_field.type)}
                )
                continue

            issue = _compare_output_type(base_field.type, candidate_field.type)
            if issue is not None:
                code, reason = issue
                report["violations"].append(
                    {
                        "code": code,
                        "path": field_path,
                        "reason": reason,
                        "baseline_type": str(base_field.type),
                        "candidate_type": str(candidate_field.type),
                    }
                )

            base_args = base_field.args
            candidate_args = candidate_field.args
            for arg_name in sorted(candidate_args):
                candidate_arg = candidate_args[arg_name]
                arg_path = f"{field_path}({arg_name})"
                if arg_name not in base_args:
                    if _is_required_argument(candidate_arg):
                        report["added_required_arguments"].append(
                            {
                                "path": arg_path,
                                "field": field_path,
                                "argument": arg_name,
                                "type": str(candidate_arg.type),
                            }
                        )
                    continue
                base_arg = base_args[arg_name]
                issue = _compare_argument_type(base_arg.type, candidate_arg.type)
                if issue is not None:
                    code, reason = issue
                    report["violations"].append(
                        {
                            "code": code,
                            "path": arg_path,
                            "reason": reason,
                            "baseline_type": str(base_arg.type),
                            "candidate_type": str(candidate_arg.type),
                        }
                    )

    report["compatible"] = not (
        report["removed_fields"]
        or report["added_required_arguments"]
        or report["violations"]
    )
    return report
