from __future__ import annotations

import io
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from rebac import backend, memberships, roles
from rebac.actors import current_actor, set_current_actor
from rebac.backends import reset_backend
from rebac.models import PermissionAuditEvent, active_relationship_model
from rebac.relationships import write_relationships
from rebac.schema import parse_zed
from rebac.types import ObjectRef, RelationshipTuple, SubjectRef

pytestmark = pytest.mark.django_db

SCHEMA_TEXT = """
caveat during_hours(timezone string) {
    timezone == "UTC"
}

definition auth/user {}

definition auth/group {
    relation member: auth/user
}

definition access/group {
    relation member: auth/user | auth/user with during_hours | auth/group | auth/group#member
    relation owner: auth/user
}

definition storage/role {
    relation member: auth/user | auth/user with during_hours | auth/group#member
}
"""


@pytest.fixture(autouse=True, params=["denormalized", "registry"])
def _command_schema(request, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = request.param
    reset_backend()
    backend().set_schema(parse_zed(SCHEMA_TEXT))
    yield
    reset_backend()


def test_grant_role_routes_through_roles_and_is_idempotent(monkeypatch) -> None:
    grant = Mock(wraps=roles.grant)
    monkeypatch.setattr(roles, "grant", grant)

    for _ in range(2):
        output = io.StringIO()
        call_command("rebac", "grant", "storage/role:viewer", "auth/user:42", stdout=output)
        assert output.getvalue() == "storage/role:viewer#member @ auth/user:42\n"

    assert grant.call_count == 2
    assert grant.call_args.kwargs["role"] == ObjectRef("storage/role", "viewer")
    assert list(roles.members_of("storage/role:viewer")) == [SubjectRef.of("auth/user", "42")]
    assert active_relationship_model().objects.count() == 1


@pytest.mark.parametrize("container", ["storage/role:viewer", "access/group:reviewers"])
def test_revoke_prints_deleted_count_and_is_idempotent(monkeypatch, container: str) -> None:
    owner = roles if container == "storage/role:viewer" else memberships
    revoke = Mock(wraps=owner.revoke)
    monkeypatch.setattr(owner, "revoke", revoke)
    call_command("rebac", "grant", container, "auth/user:42", stdout=io.StringIO())

    for expected in ("1\n", "0\n"):
        output = io.StringIO()
        call_command("rebac", "revoke", container, "auth/user:42", stdout=output)
        assert output.getvalue() == expected

    assert not active_relationship_model().objects.exists()
    argument = "role" if owner is roles else "container"
    assert revoke.call_count == 2
    assert revoke.call_args.kwargs[argument] == ObjectRef.parse(container)


@pytest.mark.parametrize("container", ["storage/role:viewer", "access/group:reviewers"])
def test_strict_revoke_of_missing_membership_fails(container: str) -> None:
    with pytest.raises(CommandError) as exc:
        call_command("rebac", "revoke", container, "auth/user:42", "--strict", stdout=io.StringIO())
    assert str(exc.value) == f"No membership found: {container}#member @ auth/user:42"


def test_non_role_container_routes_through_memberships(monkeypatch) -> None:
    grant = Mock(wraps=memberships.grant)
    role_grant = Mock(side_effect=AssertionError("A non-role container is not a role"))
    monkeypatch.setattr(memberships, "grant", grant)
    monkeypatch.setattr(roles, "grant", role_grant)
    output = io.StringIO()

    call_command("rebac", "grant", "access/group:reviewers", "auth/group:eng#member", stdout=output)

    assert output.getvalue() == "access/group:reviewers#member @ auth/group:eng#member\n"
    grant.assert_called_once()
    role_grant.assert_not_called()
    assert grant.call_args.kwargs["container"] == ObjectRef("access/group", "reviewers")
    assert list(memberships.members_of("access/group:reviewers")) == [
        SubjectRef.of("auth/group", "eng", "member")
    ]


@pytest.mark.parametrize("container", ["storage/role:viewer", "access/group:reviewers"])
def test_caveat_round_trip_and_exact_revoke(container: str) -> None:
    call_command("rebac", "grant", container, "auth/user:42", stdout=io.StringIO())
    output = io.StringIO()
    call_command(
        "rebac",
        "grant",
        container,
        "auth/user:42",
        "--caveat",
        "during_hours",
        "--caveat-context",
        '{"timezone": "UTC"}',
        stdout=output,
    )

    assert output.getvalue() == f"{container}#member @ auth/user:42 with during_hours\n"
    row = active_relationship_model().objects.get(caveat_name="during_hours")
    assert row.caveat_context == {"timezone": "UTC"}
    listing = io.StringIO()
    call_command("rebac", "relationships", "--resource", container, stdout=listing)
    assert listing.getvalue().splitlines() == [
        f"{container}#member @ auth/user:42",
        f"{container}#member @ auth/user:42 with during_hours",
    ]

    revoked = io.StringIO()
    call_command(
        "rebac", "revoke", container, "auth/user:42", "--caveat", "during_hours", stdout=revoked
    )
    assert revoked.getvalue() == "1\n"
    assert active_relationship_model().objects.get().caveat_name == ""


@pytest.fixture
def relationship_rows():
    rows = [
        ("access/group:z", "member", "auth/user:42"),
        ("auth/group:eng", "member", "auth/user:42"),
        ("access/group:a", "owner", "auth/user:7"),
        ("access/group:a", "member", "auth/group:eng#member"),
        ("access/group:a", "member", "auth/group:eng"),
    ]
    write_relationships(
        RelationshipTuple(ObjectRef.parse(resource), relation, SubjectRef.parse(subject))
        for resource, relation, subject in rows
    )
    return [
        "access/group:a#member @ auth/group:eng",
        "access/group:a#member @ auth/group:eng#member",
        "access/group:a#owner @ auth/user:7",
        "access/group:z#member @ auth/user:42",
        "auth/group:eng#member @ auth/user:42",
    ]


@pytest.mark.parametrize(
    ("flags", "indices"),
    [
        ([], [0, 1, 2, 3, 4]),
        (["--resource", "access/group:a"], [0, 1, 2]),
        (["--subject", "auth/user:42"], [3, 4]),
        (["--subject", "auth/group:eng"], [0]),
        (["--subject", "auth/group:eng#member"], [1]),
        (["--relation", "owner"], [2]),
        (
            ["--resource", "access/group:a", "--subject", "auth/group:eng", "--relation", "member"],
            [0],
        ),
        (["--limit", "2"], [0, 1]),
        (["--limit", "0"], []),
    ],
)
def test_relationships_lists_exact_filters_in_deterministic_order(
    relationship_rows, flags: list[str], indices: list[int]
) -> None:
    for _ in range(2):
        output = io.StringIO()
        call_command("rebac", "relationships", *flags, stdout=output)
        assert output.getvalue().splitlines() == [relationship_rows[index] for index in indices]
    assert active_relationship_model().objects.count() == len(relationship_rows)


@pytest.mark.parametrize(
    ("flags", "indices"),
    [
        (["--resource", "access/group:a"], [0, 1, 2]),
        (["--subject", "auth/group:eng"], [0]),
        (["--subject", "auth/group:eng#member"], [1]),
        (["--resource", "unknown/group:viewer"], []),
        (["--subject", "unknown/user:42"], []),
    ],
)
def test_relationships_lists_orphans_without_loading_schema(
    monkeypatch, relationship_rows, flags: list[str], indices: list[int]
) -> None:
    engine = backend()
    engine.set_schema(parse_zed("definition replacement/type {}"))
    schema = Mock(side_effect=AssertionError("Tuple listing must not load the schema"))
    monkeypatch.setattr(engine, "schema", schema)
    output = io.StringIO()

    call_command("rebac", "relationships", *flags, stdout=output)

    assert output.getvalue().splitlines() == [relationship_rows[index] for index in indices]
    schema.assert_not_called()


def test_relationships_lists_rows_in_one_query(
    relationship_rows, django_assert_num_queries
) -> None:
    output = io.StringIO()

    with django_assert_num_queries(1):
        call_command("rebac", "relationships", stdout=output)

    assert output.getvalue().splitlines() == relationship_rows


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["grant", "storage/role", "auth/user:42"], "Invalid role spec"),
        (["grant", "storage/role:", "auth/user:42"], "Invalid role spec"),
        (["revoke", "access/group", "auth/user:42"], "Invalid ObjectRef"),
        (["grant", "storage/role:viewer", "auth/user"], "Invalid ObjectRef"),
        (["revoke", "storage/role:viewer", "auth/user"], "Invalid ObjectRef"),
        (["relationships", "--resource", "access/group"], "Invalid ObjectRef"),
        (["relationships", "--subject", "auth/user"], "Invalid ObjectRef"),
        (["relationships", "--limit", "-1"], "--limit"),
        (["grant", "unknown/role:viewer", "auth/user:42"], "(?i)unknown resource type"),
        (["revoke", "unknown/group:viewer", "auth/user:42"], "(?i)unknown resource type"),
        (["grant", "storage/role:viewer", "unknown/user:42"], "(?i)unknown resource type"),
        (["revoke", "storage/role:viewer", "unknown/user:42"], "(?i)unknown resource type"),
    ],
)
def test_invalid_arguments_raise_command_error(args: list[str], message: str) -> None:
    with pytest.raises(CommandError, match=message):
        call_command("rebac", *args, stdout=io.StringIO())
    assert not active_relationship_model().objects.exists()


@pytest.mark.parametrize("container", ["storage/role:viewer", "access/group:reviewers"])
@pytest.mark.parametrize("caveat_flags", [[], ["--caveat", ""]])
def test_caveat_context_requires_caveat_before_writing(
    django_capture_on_commit_callbacks, container: str, caveat_flags: list[str]
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(CommandError, match="--caveat"):
            call_command(
                "rebac",
                "grant",
                container,
                "auth/user:42",
                *caveat_flags,
                "--caveat-context",
                '{"timezone": "UTC"}',
                stdout=io.StringIO(),
            )

    assert not active_relationship_model().objects.exists()
    assert not PermissionAuditEvent.objects.exists()


@pytest.mark.parametrize(
    ("context", "message"),
    [("{", "valid JSON"), ("[]", "JSON object"), ("null", "JSON object")],
)
def test_invalid_caveat_context_fails_before_writing(context: str, message: str) -> None:
    with pytest.raises(CommandError, match=message):
        call_command(
            "rebac",
            "grant",
            "storage/role:viewer",
            "auth/user:42",
            "--caveat",
            "during_hours",
            "--caveat-context",
            context,
            stdout=io.StringIO(),
        )
    assert not active_relationship_model().objects.exists()


@pytest.mark.parametrize("actor", [None, SubjectRef.of("auth/user", "admin")])
def test_grant_and_revoke_preserve_ambient_audit_actor(
    django_capture_on_commit_callbacks, actor: SubjectRef | None
) -> None:
    previous = current_actor()
    set_current_actor(actor)
    try:
        with django_capture_on_commit_callbacks(execute=True):
            for verb in ("grant", "revoke"):
                call_command(
                    "rebac", verb, "storage/role:viewer", "auth/user:42", stdout=io.StringIO()
                )
    finally:
        set_current_actor(previous)

    events = list(PermissionAuditEvent.objects.order_by("pk"))
    assert [event.kind for event in events] == [
        PermissionAuditEvent.KIND_RELATIONSHIP_GRANT,
        PermissionAuditEvent.KIND_RELATIONSHIP_REVOKE,
    ]
    for event in events:
        assert event.actor_subject_type == (actor.subject_type if actor else "")
        assert event.actor_subject_id == (actor.subject_id if actor else "")
        assert event.target_repr == "storage/role:viewer#member @ auth/user:42"
