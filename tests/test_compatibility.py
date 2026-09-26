"""Tests for the schema compatibility checker."""

from __future__ import annotations

from app.compatibility import check_schema_compatibility


BASELINE = """
type Query {
  user(id: ID!): User
  users(first: Int): [User!]!
}

interface Node {
  id: ID!
}

type User implements Node {
  id: ID!
  name: String
  email: String!
  friends: [User]
  tags: [String]
}

type Repository implements Node {
  id: ID!
  name: String
}
"""


def check(candidate: str, baseline: str = BASELINE):
    return check_schema_compatibility(
        baseline_sdl=baseline, candidate_sdl=candidate
    )


def codes_by_path(result):
    return {v["path"]: v["code"] for v in result["violations"]}


# --------------------------------------------------------------------------- #
# Compatible changes
# --------------------------------------------------------------------------- #


def test_identical_schema_is_compatible():
    result = check(BASELINE)
    assert result["status"] == "ok"
    assert result["compatible"] is True
    assert result["violations"] == []
    assert result["errors"] == []
    assert result["summary"]["violation_count"] == 0
    assert result["summary"]["checked_field_count"] == 10


def test_additive_changes_are_compatible():
    candidate = """
    type Query {
      user(id: ID!): User
      users(first: Int, limit: Int = 10, after: String): [User!]!
      newField: Boolean
    }
    interface Node { id: ID! }
    type User implements Node {
      id: ID!
      name: String
      email: String!
      friends: [User]
      tags: [String]
      nickname: String
    }
    type Repository implements Node { id: ID! name: String }
    type BrandNew { id: ID! }
    """
    result = check(candidate)
    assert result["status"] == "ok"
    assert result["violations"] == []


def test_return_type_may_strengthen_non_null():
    candidate = BASELINE.replace("name: String", "name: String!").replace(
        "friends: [User]", "friends: [User!]!"
    )
    result = check(candidate)
    assert result["status"] == "ok"
    assert result["violations"] == []


def test_argument_type_may_relax_non_null():
    baseline = "type Query { users(first: Int!, filter: [String!]!): Boolean }"
    candidate = "type Query { users(first: Int, filter: [String]): Boolean }"
    result = check(candidate, baseline)
    assert result["status"] == "ok"
    assert result["violations"] == []


# --------------------------------------------------------------------------- #
# Removed fields
# --------------------------------------------------------------------------- #


def test_removed_field_is_reported():
    candidate = BASELINE.replace("  email: String!\n", "")
    result = check(candidate)
    assert result["status"] == "incompatible"
    assert result["compatible"] is False
    by_path = codes_by_path(result)
    assert by_path["User.email"] == "FIELD_REMOVED"
    assert result["summary"]["removed_field_count"] == 1


def test_removed_type_reports_its_fields():
    # Removing the Repository type means every one of its baseline fields is
    # unavailable to existing clients.
    candidate = BASELINE.replace(
        "type Repository implements Node {\n  id: ID!\n  name: String\n}\n", ""
    )
    result = check(candidate)
    by_path = codes_by_path(result)
    assert by_path["Repository.id"] == "FIELD_REMOVED"
    assert by_path["Repository.name"] == "FIELD_REMOVED"


def test_interface_field_removal_is_reported():
    # Node loses `id` and gains `name`; both object types already define
    # `name`, so the candidate SDL itself stays valid.
    candidate = BASELINE.replace(
        "interface Node {\n  id: ID!\n}", "interface Node {\n  name: String\n}"
    )
    result = check(candidate)
    assert codes_by_path(result)["Node.id"] == "FIELD_REMOVED"


# --------------------------------------------------------------------------- #
# Required arguments
# --------------------------------------------------------------------------- #


def test_required_argument_added_is_reported():
    candidate = BASELINE.replace(
        "users(first: Int): [User!]!",
        "users(first: Int, filter: String!): [User!]!",
    )
    result = check(candidate)
    by_path = codes_by_path(result)
    assert by_path["Query.users(filter)"] == "REQUIRED_ARGUMENT_ADDED"
    violation = next(
        v for v in result["violations"]
        if v["code"] == "REQUIRED_ARGUMENT_ADDED"
    )
    assert violation["details"] == {"argument_type": "String!"}
    assert result["summary"]["required_argument_count"] == 1


def test_required_argument_with_default_is_optional():
    candidate = BASELINE.replace(
        "users(first: Int): [User!]!",
        "users(first: Int, limit: Int! = 10): [User!]!",
    )
    result = check(candidate)
    assert result["status"] == "ok"
    assert result["violations"] == []


def test_nullable_new_argument_is_not_required():
    candidate = BASELINE.replace(
        "user(id: ID!): User", "user(id: ID!, comment: String): User"
    )
    result = check(candidate)
    assert result["status"] == "ok"


# --------------------------------------------------------------------------- #
# Return type changes
# --------------------------------------------------------------------------- #


def test_return_named_type_change_is_reported():
    candidate = BASELINE.replace(
        "user(id: ID!): User", "user(id: ID!): Repository"
    )
    result = check(candidate)
    by_path = codes_by_path(result)
    assert by_path["Query.user"] == "RETURN_NAMED_TYPE_CHANGED"
    violation = next(v for v in result["violations"] if v["path"] == "Query.user")
    assert violation["details"]["baseline_type"] == "User"
    assert violation["details"]["candidate_type"] == "Repository"


def test_return_list_depth_change_is_reported():
    candidate = BASELINE.replace("tags: [String]", "tags: [[String]]")
    result = check(candidate)
    assert codes_by_path(result)["User.tags"] == "RETURN_LIST_DEPTH_CHANGED"


def test_scalar_become_list_is_depth_change():
    candidate = BASELINE.replace("name: String", "name: [String]")
    result = check(candidate)
    assert codes_by_path(result)["User.name"] == "RETURN_LIST_DEPTH_CHANGED"


def test_return_nullability_weakened_on_named_level():
    candidate = BASELINE.replace("email: String!", "email: String")
    result = check(candidate)
    assert codes_by_path(result)["User.email"] == "RETURN_NULLABILITY_WEAKENED"


def test_return_nullability_weakened_on_list_item_level():
    candidate = BASELINE.replace("users(first: Int): [User!]!", "users(first: Int): [User]!")
    result = check(candidate)
    violation = next(
        v for v in result["violations"] if v["path"] == "Query.users"
    )
    assert violation["code"] == "RETURN_NULLABILITY_WEAKENED"
    # Position 0 is the named/item level, position 1 is the list wrapper.
    assert violation["details"]["positions"] == [0]


def test_return_nullability_weakened_on_outer_wrapper():
    candidate = BASELINE.replace("friends: [User]", "friends: [User]!")
    # [User] -> [User]! strengthens the outer wrapper (safe); the reverse breaks.
    assert check(candidate)["status"] == "ok"
    candidate_loose = BASELINE.replace(
        "users(first: Int): [User!]!", "users(first: Int): [User!]"
    )
    violation = next(
        v for v in check(candidate_loose)["violations"]
        if v["path"] == "Query.users"
    )
    assert violation["code"] == "RETURN_NULLABILITY_WEAKENED"
    assert violation["details"]["positions"] == [1]


# --------------------------------------------------------------------------- #
# Argument type changes
# --------------------------------------------------------------------------- #


def test_argument_named_type_change_is_reported():
    candidate = BASELINE.replace(
        "user(id: ID!)", "user(id: String!)"
    )
    result = check(candidate)
    assert (
        codes_by_path(result)["Query.user(id)"]
        == "ARGUMENT_NAMED_TYPE_CHANGED"
    )


def test_argument_nullability_strengthened_is_reported():
    candidate = BASELINE.replace("users(first: Int)", "users(first: Int!)")
    result = check(candidate)
    violation = next(
        v for v in result["violations"]
        if v["path"] == "Query.users(first)"
    )
    assert violation["code"] == "ARGUMENT_NULLABILITY_STRENGTHENED"
    assert violation["details"]["positions"] == [0]


def test_argument_list_item_nullability_strengthened():
    baseline = "type Query { q(f: [String]): Boolean }"
    candidate = "type Query { q(f: [String!]): Boolean }"
    result = check(candidate, baseline)
    violation = result["violations"][0]
    assert violation["code"] == "ARGUMENT_NULLABILITY_STRENGTHENED"
    assert violation["details"]["positions"] == [0]


def test_argument_list_depth_change_is_reported():
    baseline = "type Query { q(f: [String]): Boolean }"
    candidate = "type Query { q(f: [[String]]): Boolean }"
    result = check(candidate, baseline)
    assert result["violations"][0]["code"] == "ARGUMENT_LIST_DEPTH_CHANGED"


# --------------------------------------------------------------------------- #
# Reporting / errors
# --------------------------------------------------------------------------- #


def test_violations_are_sorted_by_type_path():
    candidate = """
    type Query {
      user(id: ID!): User
      users(first: Int, filter: String!): [User!]!
    }
    interface Node { id: ID! }
    type User implements Node { id: ID! name: String }
    type Repository implements Node { id: ID! }
    """
    result = check(candidate)
    paths = [v["path"] for v in result["violations"]]
    assert paths == sorted(paths)
    assert "Query.users(filter)" in paths
    assert "Repository.name" in paths
    assert "User.email" in paths
    # Fields still present under the same name are not reported.
    assert "Node.id" not in paths
    assert "Repository.id" not in paths


def test_invalid_baseline_sdl():
    result = check_schema_compatibility(
        baseline_sdl="type Query {", candidate_sdl=BASELINE
    )
    assert result["status"] == "invalid"
    assert result["compatible"] is False
    assert result["summary"] is None
    assert result["errors"][0]["code"] == "INVALID_BASELINE_SDL"


def test_invalid_candidate_sdl():
    result = check_schema_compatibility(
        baseline_sdl=BASELINE, candidate_sdl="type Query { x: "
    )
    assert result["status"] == "invalid"
    assert result["errors"][0]["code"] == "INVALID_CANDIDATE_SDL"


def test_summary_counts_violation_categories():
    candidate = """
    type Query {
      users(first: Int!): String
    }
    interface Node { id: ID! }
    type User implements Node { id: ID! }
    type Repository implements Node { id: ID! name: String }
    """
    result = check(candidate)
    summary = result["summary"]
    assert summary["compatible"] is False
    # Query.user and four of User's five fields are missing.
    assert summary["removed_field_count"] == 5
    assert summary["required_argument_count"] == 0  # first existed in baseline
    # Query.users: named type + list depth changed; first: Int -> Int!.
    assert summary["type_violation_count"] == 3
    assert summary["violation_count"] == len(result["violations"])
