"""Tests for baseline-vs-candidate schema compatibility checks."""

from __future__ import annotations

import pytest

from app.compatibility import check_schema_compatibility

BASELINE = """
type Query {
  user(id: ID!): User
  search(text: String, first: Int): [User!]!
  node(id: ID!): Node
  deprecated: String
}

interface Node {
  id: ID!
}

type User implements Node {
  id: ID!
  name: String
  email: String
  tags: [String!]
  matrix: [[Int!]!]!
  friends(first: Int = 10): [User!]!
}
"""


def check(candidate_sdl: str, baseline_sdl: str = BASELINE):
    return check_schema_compatibility(
        baseline_sdl=baseline_sdl, candidate_sdl=candidate_sdl
    )


def codes(report) -> list[str]:
    return [v["code"] for v in report["violations"]]


def paths(items) -> list[str]:
    return [i["path"] for i in items]


# --------------------------------------------------------------------------- #
# Compatible changes
# --------------------------------------------------------------------------- #


def test_identical_schema_is_compatible():
    report = check(BASELINE)
    assert report["compatible"] is True
    assert report["violations"] == []
    assert report["removed_fields"] == []
    assert report["added_required_arguments"] == []
    assert report["errors"] == []


def test_purely_additive_schema_is_compatible():
    candidate = BASELINE + """
    type Extra { value: Int }
    extend type Query { extra: Extra }
    extend type User { nickname: String }
    """
    report = check(candidate)
    assert report["compatible"] is True


def test_return_type_strengthened_nullability_is_compatible():
    candidate = BASELINE.replace("name: String", "name: String!")
    report = check(candidate)
    assert report["compatible"] is True


def test_list_nullability_strengthened_at_any_level_is_compatible():
    # [String!] -> [String!]! (outer list non-null) and
    # [User!]! friends stays; matrix [[Int!]!]! already fully strict.
    candidate = BASELINE.replace("tags: [String!]", "tags: [String!]!")
    report = check(candidate)
    assert report["compatible"] is True


def test_optional_and_defaulted_new_arguments_are_compatible():
    candidate = BASELINE.replace(
        "search(text: String, first: Int)",
        "search(text: String, first: Int, after: ID, limit: Int = 20)",
    )
    report = check(candidate)
    assert report["compatible"] is True


def test_argument_nullability_loosened_is_compatible():
    candidate = BASELINE.replace("user(id: ID!)", "user(id: ID)")
    report = check(candidate)
    assert report["compatible"] is True


def test_argument_list_nullability_loosened_is_compatible():
    baseline = "type Query { f(a: [Int!]!): Int }"
    candidate = "type Query { f(a: [Int!]): Int }"
    report = check(candidate, baseline_sdl=baseline)
    assert report["compatible"] is True


# --------------------------------------------------------------------------- #
# Removed fields
# --------------------------------------------------------------------------- #


def test_removed_field_is_reported():
    candidate = BASELINE.replace("  email: String\n", "")
    report = check(candidate)
    assert report["compatible"] is False
    assert paths(report["removed_fields"]) == ["User.email"]
    assert report["removed_fields"][0]["type"] == "String"


def test_removed_root_field_is_reported():
    candidate = BASELINE.replace("  deprecated: String\n", "")
    report = check(candidate)
    assert paths(report["removed_fields"]) == ["Query.deprecated"]


def test_removed_type_marks_all_its_fields_removed():
    candidate = """
    type Query {
      user(id: ID!): User
    }
    type User {
      id: ID!
    }
    """
    baseline = """
    type Query {
      user(id: ID!): User
    }
    type User {
      id: ID!
      name: String
    }
    interface Node { id: ID! }
    """
    report = check(candidate, baseline_sdl=baseline)
    assert "User.name" in paths(report["removed_fields"])
    assert "Node.id" in paths(report["removed_fields"])


def test_type_kind_change_treated_as_removed_fields():
    # User changes from object type to scalar name clash: old fields gone.
    candidate = """
    scalar User
    type Query {
      user(id: ID!): User
      search(text: String, first: Int): [User!]!
      node(id: ID!): Node
      deprecated: String
    }
    interface Node { id: ID! }
    """
    report = check(candidate)
    removed = paths(report["removed_fields"])
    assert "User.id" in removed
    assert "User.name" in removed


# --------------------------------------------------------------------------- #
# New required arguments
# --------------------------------------------------------------------------- #


def test_new_required_argument_is_reported():
    candidate = BASELINE.replace(
        "search(text: String, first: Int)",
        "search(text: String, first: Int, after: ID!)",
    )
    report = check(candidate)
    assert report["compatible"] is False
    added = report["added_required_arguments"]
    assert len(added) == 1
    assert added[0]["path"] == "Query.search(after)"
    assert added[0]["field"] == "Query.search"
    assert added[0]["argument"] == "after"
    assert added[0]["type"] == "ID!"


def test_nullable_or_defaulted_argument_is_not_required():
    candidate = BASELINE.replace(
        "user(id: ID!)", "user(id: ID!, name: String, role: Role = ADMIN)"
    )
    candidate = "enum Role { ADMIN USER }\n" + candidate
    report = check(candidate)
    assert report["added_required_arguments"] == []
    assert report["compatible"] is True


def test_explicit_null_default_is_not_required():
    baseline = "type Query { f: Int }"
    candidate = "type Query { f(x: Int = null): Int }"
    report = check(candidate, baseline_sdl=baseline)
    assert report["added_required_arguments"] == []
    assert report["compatible"] is True


def test_required_list_argument_is_reported():
    baseline = "type Query { f: Int }"
    candidate = "type Query { f(items: [String!]!): Int }"
    report = check(candidate, baseline_sdl=baseline)
    item = report["added_required_arguments"][0]
    assert item["path"] == "Query.f(items)"
    assert item["type"] == "[String!]!"


# --------------------------------------------------------------------------- #
# Return-type violations
# --------------------------------------------------------------------------- #


def test_named_return_type_change_is_violation():
    candidate = BASELINE.replace("user(id: ID!): User", "user(id: ID!): Account")
    candidate = candidate + "\ntype Account { id: ID! }\n"
    report = check(candidate)
    violation = next(v for v in report["violations"] if v["path"] == "Query.user")
    assert violation["code"] == "FIELD_NAMED_TYPE_CHANGED"
    assert "User" in violation["reason"] and "Account" in violation["reason"]
    assert violation["baseline_type"] == "User"
    assert violation["candidate_type"] == "Account"


def test_list_depth_change_is_violation():
    baseline = "type Query { f: [String] }"
    candidate = "type Query { f: String }"
    report = check(candidate, baseline_sdl=baseline)
    assert codes(report) == ["FIELD_LIST_DEPTH_CHANGED"]

    baseline2 = "type Query { f: [[String]] }"
    candidate2 = "type Query { f: [String] }"
    report2 = check(candidate2, baseline_sdl=baseline2)
    assert codes(report2) == ["FIELD_LIST_DEPTH_CHANGED"]


def test_nullable_return_becomes_list_depth_change():
    # String -> [String] changes nesting even though named type is same.
    report = check("type Query { f: String }", baseline_sdl="type Query { f: [String] }")
    assert codes(report) == ["FIELD_LIST_DEPTH_CHANGED"]


def test_return_nullability_weakened_named_type():
    candidate = BASELINE.replace("id: ID!", "id: ID")
    report = check(candidate)
    # Both Query arg unaffected; User.id and Node.id (interface) change.
    violations = [v for v in report["violations"] if v["code"] == "FIELD_NULLABILITY_WEAKENED"]
    assert {v["path"] for v in violations} == {"Node.id", "User.id"}
    assert "null" in violations[0]["reason"].lower()


def test_return_nullability_weakened_outer_list():
    # search returns [User!]! -> [User!]
    candidate = BASELINE.replace("search(text: String, first: Int): [User!]!",
                                 "search(text: String, first: Int): [User!]")
    report = check(candidate)
    violation = next(v for v in report["violations"] if v["path"] == "Query.search")
    assert violation["code"] == "FIELD_NULLABILITY_WEAKENED"
    assert "list" in violation["reason"]


def test_return_nullability_weakened_inner_named_type():
    # friends: [User!]! -> [User]! drops the non-null element guarantee.
    candidate = BASELINE.replace("friends(first: Int = 10): [User!]!",
                                 "friends(first: Int = 10): [User]!")
    report = check(candidate)
    violation = next(v for v in report["violations"] if v["path"] == "User.friends")
    assert violation["code"] == "FIELD_NULLABILITY_WEAKENED"


# --------------------------------------------------------------------------- #
# Argument-type violations
# --------------------------------------------------------------------------- #


def test_existing_argument_named_type_changed():
    candidate = BASELINE.replace("user(id: ID!)", "user(id: String!)")
    report = check(candidate)
    violation = report["violations"][0]
    assert violation["path"] == "Query.user(id)"
    assert violation["code"] == "ARGUMENT_NAMED_TYPE_CHANGED"


def test_existing_argument_nullability_strengthened():
    candidate = BASELINE.replace("search(text: String", "search(text: String!")
    report = check(candidate)
    violation = report["violations"][0]
    assert violation["path"] == "Query.search(text)"
    assert violation["code"] == "ARGUMENT_NULLABILITY_STRENGTHENED"
    assert violation["baseline_type"] == "String"
    assert violation["candidate_type"] == "String!"


def test_argument_list_nullability_strengthened():
    baseline = "type Query { f(a: [Int]): Int }"
    candidate = "type Query { f(a: [Int]!): Int }"
    report = check(candidate, baseline_sdl=baseline)
    assert codes(report) == ["ARGUMENT_NULLABILITY_STRENGTHENED"]


def test_argument_list_depth_changed():
    baseline = "type Query { f(a: [Int]): Int }"
    candidate = "type Query { f(a: [[Int]]): Int }"
    report = check(candidate, baseline_sdl=baseline)
    assert codes(report) == ["ARGUMENT_LIST_DEPTH_CHANGED"]


def test_removed_argument_is_not_flagged():
    # Existing requests still send the argument; GraphQL ignores unknown
    # arguments on field definitions — removal is intentionally out of scope.
    candidate = BASELINE.replace("search(text: String, first: Int)", "search(text: String)")
    report = check(candidate)
    assert report["compatible"] is True


# --------------------------------------------------------------------------- #
# Combined report + invalid SDL
# --------------------------------------------------------------------------- #


def test_report_collects_multiple_problems():
    candidate = """
    type Query {
      user(id: ID!): Account
      search(text: String!, first: Int, after: ID!): [User!]
    }
    type Account { id: ID! }
    type User { id: ID! }
    """
    report = check(candidate)
    assert report["compatible"] is False
    # Query.node and Query.deprecated removed; interface Node removed too.
    assert "Query.node" in paths(report["removed_fields"])
    assert "Query.deprecated" in paths(report["removed_fields"])
    assert "Query.search(after)" in paths(report["added_required_arguments"])
    by_path = {v["path"]: v["code"] for v in report["violations"]}
    assert by_path["Query.user"] == "FIELD_NAMED_TYPE_CHANGED"
    assert by_path["Query.search(text)"] == "ARGUMENT_NULLABILITY_STRENGTHENED"
    assert by_path["Query.search"] == "FIELD_NULLABILITY_WEAKENED"
    # User lost many fields, interface Node vanished:
    assert "User.email" in paths(report["removed_fields"])


def test_invalid_baseline_sdl_is_reported_not_raised():
    report = check_schema_compatibility(
        baseline_sdl="type Query {", candidate_sdl="type Query { f: Int }"
    )
    assert report["compatible"] is False
    error = report["errors"][0]
    assert error["code"] == "GRAPHQL_ERROR"
    assert error["details"]["side"] == "baseline"


def test_invalid_candidate_sdl_is_reported_not_raised():
    report = check_schema_compatibility(
        baseline_sdl="type Query { f: Int }", candidate_sdl="type Query {"
    )
    assert report["compatible"] is False
    assert report["errors"][0]["details"]["side"] == "candidate"


@pytest.mark.parametrize(
    "baseline_type,candidate_type,code",
    [
        ("String", "String!", None),
        ("String!", "String", "FIELD_NULLABILITY_WEAKENED"),
        ("[String]", "[String!]", None),
        ("[String!]", "[String]", "FIELD_NULLABILITY_WEAKENED"),
        ("[String]!", "[String]", "FIELD_NULLABILITY_WEAKENED"),
        ("[[Int!]!]!", "[[Int!]]!", "FIELD_NULLABILITY_WEAKENED"),
        ("[Int]", "Int", "FIELD_LIST_DEPTH_CHANGED"),
        ("Int", "Int!", None),
    ],
)
def test_return_type_rule_matrix(baseline_type, candidate_type, code):
    baseline = f"type Query {{ f: {baseline_type} }}"
    candidate = f"type Query {{ f: {candidate_type} }}"
    report = check(candidate, baseline_sdl=baseline)
    expected = [code] if code else []
    assert codes(report) == expected
