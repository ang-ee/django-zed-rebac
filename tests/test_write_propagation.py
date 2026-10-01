"""A tuple, model or schema write reaches every permission that depends on it."""

from __future__ import annotations

import random
from contextlib import nullcontext
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from rebac import (
    ObjectRef,
    RelationshipTuple,
    SubjectRef,
    backend,
    schema_changes,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.models import (
    Relationship,
    SchemaDefinition,
    SchemaOverride,
    SchemaPermission,
    SchemaRelation,
    active_relationship_model,
)
from rebac.testing import install_schema
from tests.reference_harness import assert_reads_match
from tests.testapp.models import Folder, Post

pytestmark = pytest.mark.django_db

USER = SubjectRef.of("auth/user", "alice")
BOB = SubjectRef.of("auth/user", "bob")
SCHEMA = """
caveat gate(ok bool) { ok }
definition auth/user {}
definition test/leaf {
    relation viewer: auth/user
    permission read = viewer
}
definition test/thread {
    relation viewer: auth/user | auth/user with gate
    relation follower: auth/user
    permission read = viewer
    permission follow = follower
}
definition test/message {
    relation thread: test/thread
    permission read = thread->read
}
definition test/role {
    relation member: auth/user
    relation includes: test/role
    permission effective_member = member + includes->effective_member
}
definition test/doc {
    relation role: test/role
    permission read = role->effective_member
}
definition test/group {
    relation member: auth/user | test/group#member
}
definition test/groupdoc {
    relation viewer: auth/user | test/group#member
    permission read = viewer
}
definition test/expiring {
    relation viewer: auth/user with gate with expiration
    permission read = viewer
}
definition test/expiringdoc {
    relation parent: test/expiring
    permission read = parent->read
}
"""


@pytest.fixture
def active():
    return install_schema(SCHEMA)


def row(type_: str, id_: str, relation: str, subject: SubjectRef = USER, **metadata):
    return RelationshipTuple(ObjectRef(type_, id_), relation, subject, **metadata)


def read_at(local, resource: ObjectRef, action: str, when, subject: SubjectRef = USER):
    """A point check with the application clock at ``when``."""
    with patch("django.utils.timezone.now", return_value=when):
        return local.check_access(subject=subject, resource=resource, action=action)


def test_own_object_write_grants_that_object_only(active):
    active.write_relationships([row("test/leaf", str(n), "viewer") for n in range(8)])
    active.write_relationships([row("test/leaf", "fresh", "viewer")])
    assert active.check_access(
        subject=USER, resource=ObjectRef("test/leaf", "fresh"), action="read"
    ).allowed
    assert set(active.accessible(subject=USER, action="read", resource_type="test/leaf")) == {
        "fresh",
        *(str(n) for n in range(8)),
    }
    assert_reads_match(
        subjects=[USER, BOB],
        resources=[ObjectRef("test/leaf", name) for name in ("fresh", "0", "7", "absent")],
        actions=["read"],
    )


def test_rewriting_a_stored_tuple_keeps_dependent_reads(active):
    initial = row("test/thread", "root", "viewer")
    messages = [ObjectRef("test/message", str(n)) for n in range(12)]
    active.write_relationships(
        [initial]
        + [
            row("test/message", str(n), "thread", SubjectRef.of("test/thread", "root"))
            for n in range(12)
        ]
    )
    active.write_relationships([initial])
    assert all(
        active.check_access(subject=USER, resource=message, action="read").allowed
        for message in messages
    )
    assert_reads_match(
        subjects=[USER, BOB],
        resources=[ObjectRef("test/thread", "root"), *messages],
        actions=["read"],
    )


def test_fan_in_readers_follow_only_the_permission_they_read(active):
    thread = ObjectRef("test/thread", "root")
    messages = [ObjectRef("test/message", str(n)) for n in range(20)]
    active.write_relationships(
        [row("test/thread", "root", "viewer")]
        + [
            row("test/message", str(n), "thread", SubjectRef.of("test/thread", "root"))
            for n in range(20)
        ]
    )
    active.write_relationships(
        [row("test/thread", "root", "follower"), row("test/thread", "root", "follower", BOB)]
    )
    for subject in (USER, BOB):
        assert active.check_access(subject=subject, resource=thread, action="follow").allowed
    assert all(
        active.check_access(subject=USER, resource=message, action="read").allowed
        for message in messages
    )
    assert not any(
        active.check_access(subject=BOB, resource=message, action="read").allowed
        for message in messages
    )
    assert_reads_match(subjects=[USER, BOB], resources=[thread, *messages], actions=["read"])


def test_revocation_crosses_recursive_component(active):
    root = row("test/role", "root", "member")
    document = ObjectRef("test/doc", "one")
    active.write_relationships(
        [
            root,
            row("test/role", "child", "includes", SubjectRef.of("test/role", "root")),
            row("test/role", "grandchild", "includes", SubjectRef.of("test/role", "child")),
            row("test/doc", "one", "role", SubjectRef.of("test/role", "grandchild")),
        ]
    )
    assert active.check_access(subject=USER, resource=document, action="read").allowed
    active.delete_relationship(root)
    assert not active.check_access(subject=USER, resource=document, action="read").allowed
    for name in ("root", "child", "grandchild"):
        assert not active.check_access(
            subject=USER, resource=ObjectRef("test/role", name), action="effective_member"
        ).allowed


def test_recursive_cycle_revocation_removes_old_fixpoint(active):
    root = row("test/role", "root", "member")
    active.write_relationships(
        [
            root,
            row("test/role", "child", "includes", SubjectRef.of("test/role", "root")),
            row("test/role", "root", "includes", SubjectRef.of("test/role", "child")),
            row("test/doc", "one", "role", SubjectRef.of("test/role", "child")),
        ]
    )
    active.delete_relationship(root)
    assert not active.check_access(
        subject=USER, resource=ObjectRef("test/doc", "one"), action="read"
    ).allowed


def test_membership_revocation_reaches_containers_and_keeps_other_members(active):
    child = row("test/group", "child", "member")
    bob = SubjectRef.of("auth/user", "bob")
    active.write_relationships(
        [
            child,
            row("test/group", "child", "member", bob),
            row("test/group", "parent", "member", SubjectRef.of("test/group", "child", "member")),
            row("test/groupdoc", "one", "viewer", SubjectRef.of("test/group", "parent", "member")),
        ]
    )
    assert active.check_access(
        subject=USER, resource=ObjectRef("test/groupdoc", "one"), action="read"
    ).allowed
    active.delete_relationship(child)
    assert not active.check_access(
        subject=USER, resource=ObjectRef("test/groupdoc", "one"), action="read"
    ).allowed
    assert active.check_access(
        subject=bob, resource=ObjectRef("test/groupdoc", "one"), action="read"
    ).allowed


def test_direct_grant_revocation_preserves_independent_subject_set_grant(active):
    bob = SubjectRef.of("auth/user", "bob")
    direct = row("test/groupdoc", "one", "viewer")
    active.write_relationships(
        [
            row("test/group", "child", "member", bob),
            row("test/groupdoc", "one", "viewer", SubjectRef.of("test/group", "child", "member")),
            direct,
        ]
    )
    active.delete_relationship(direct)
    assert active.check_access(
        subject=bob, resource=ObjectRef("test/groupdoc", "one"), action="read"
    ).allowed


@pytest.mark.parametrize("length", [3, 5])
@pytest.mark.parametrize("change", ["revoke", "grant"])
def test_membership_cycle_change_reaches_fixpoint(active, length, change):
    names = [f"g{n}" for n in range(length)]
    links = [
        row(
            "test/group",
            name,
            "member",
            SubjectRef.of("test/group", names[(n + 1) % length], "member"),
        )
        for n, name in enumerate(names)
    ]
    direct = row("test/group", names[0], "member")
    viewer = row("test/groupdoc", "one", "viewer", SubjectRef.of("test/group", names[-1], "member"))
    active.write_relationships([*links, viewer, *([direct] if change == "revoke" else [])])
    if change == "revoke":
        active.delete_relationship(direct)
    else:
        active.write_relationships([direct])
    assert active.check_access(
        subject=USER, resource=ObjectRef("test/groupdoc", "one"), action="read"
    ).allowed is (change == "grant")


def test_expiry_and_condition_payload_changes_propagate(active):
    early = timezone.now() + timedelta(days=1)
    late = early + timedelta(days=1)
    between = early + timedelta(hours=1)
    resources = [ObjectRef("test/expiring", "one"), ObjectRef("test/expiringdoc", "child")]

    def read_between(resource, context=None):
        with patch("django.utils.timezone.now", return_value=between):
            return active.check_access(
                subject=USER, resource=resource, action="read", context=context
            )

    active.write_relationships(
        [
            row("test/expiring", "one", "viewer", caveat_name="gate", expires_at=early),
            row("test/expiringdoc", "child", "parent", SubjectRef.of("test/expiring", "one")),
        ]
    )
    for resource in resources:
        assert not read_between(resource, {"ok": True}).allowed
    active.write_relationships(
        [row("test/expiring", "one", "viewer", caveat_name="gate", expires_at=late)]
    )
    for resource in resources:
        assert read_between(resource, {"ok": True}).allowed
        assert read_between(resource).conditional_on == ("ok",)
    active.write_relationships(
        [
            row(
                "test/expiring",
                "one",
                "viewer",
                caveat_name="gate",
                caveat_context={"ok": True},
                expires_at=late,
            )
        ]
    )
    for resource in resources:
        assert read_between(resource).allowed
    for now in (between, late):
        assert_reads_match(
            subjects=[USER, BOB],
            resources=resources,
            actions=["read"],
            contexts=[None, {"ok": True}, {"ok": False}],
            now=now,
        )


VIA_USERSET_SCHEMA = """
definition auth/user {}
definition test/team {
    relation member: auth/user | test/team#member
    relation admin: auth/user
    permission manage = admin + member->admin
}
definition test/other {
    relation viewer: auth/user
    permission read = viewer
}
"""


@pytest.mark.parametrize("change", ["grant", "revoke"])
@pytest.mark.parametrize("mixed", [False, True])
def test_arrow_via_userset_only_relation_changes_grant(change, mixed):
    local = install_schema(VIA_USERSET_SCHEMA)
    link = row("test/team", "b", "member", SubjectRef.of("test/team", "a", "member"))
    local.write_relationships([row("test/team", "a", "admin")])
    if change == "revoke":
        local.write_relationships([link])
    with transaction.atomic() if mixed else nullcontext():
        if change == "revoke":
            local.delete_relationship(link)
        else:
            local.write_relationships([link])
        if mixed:
            local.write_relationships(
                [row("test/other", "x", "viewer", SubjectRef.of("auth/user", "bob"))]
            )
    assert local.check_access(
        subject=USER, resource=ObjectRef("test/team", "b"), action="manage"
    ).allowed is (change == "grant")


TWO_TYPE_SCHEMA = """
definition auth/user {}
definition test/folder {
    relation project: test/project
    relation viewer: auth/user
    permission access = viewer
    permission view = access + project->access
}
definition test/project {
    relation folder: test/folder
    relation member: auth/user
    permission access = member + folder->view
}
definition test/doc {
    relation folder: test/folder
    permission x = folder->access
}
"""


def test_two_type_recursive_component_reads_direct_grant():
    local = install_schema(TWO_TYPE_SCHEMA)
    local.write_relationships([row("test/folder", "f", "viewer")])
    assert local.check_access(
        subject=USER, resource=ObjectRef("test/folder", "f"), action="access"
    ).allowed


def test_two_type_recursive_component_revocation_reaches_both_types():
    local = install_schema(TWO_TYPE_SCHEMA)
    local.write_relationships(
        [
            row("test/folder", "f", "viewer"),
            row("test/project", "p", "folder", SubjectRef.of("test/folder", "f")),
            row("test/folder", "g", "project", SubjectRef.of("test/project", "p")),
        ]
    )
    local.delete_relationship(row("test/folder", "f", "viewer"))
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/folder", "g"), action="view"
    ).allowed
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/project", "p"), action="access"
    ).allowed


def test_two_type_recursive_component_revoke_reaches_arrow_reader():
    local = install_schema(TWO_TYPE_SCHEMA)
    local.write_relationships(
        [
            row("test/folder", "f", "viewer"),
            row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f")),
        ]
    )
    local.delete_relationship(row("test/folder", "f", "viewer"))
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="x"
    ).allowed


CONST_USERSET_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation everyone: auth/user // rebac:const=alice
    relation parent: blog/folder // rebac:field=parent
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition blog/post {
    relation viewer: auth/user | blog/folder#everyone
    permission read = viewer
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
"""


def test_new_model_row_grants_through_const_subject_set():
    local = install_schema(CONST_USERSET_SCHEMA)
    with sudo(reason="write propagation test"):
        folder = Folder.objects.create(name="new")
    local.write_relationships(
        [
            row(
                "blog/post",
                "p",
                "viewer",
                SubjectRef.of("blog/folder", str(folder.pk), "everyone"),
            )
        ]
    )
    assert local.check_access(
        subject=USER, resource=ObjectRef("blog/post", "p"), action="read"
    ).allowed


DIFFERENTIAL_SCHEMA = """
caveat gate(ok bool) { ok }
definition auth/user {}
definition test/group {
    relation member: auth/user | test/group#member
}
definition test/team {
    relation member: auth/user | test/team#member
    relation admin: auth/user
    permission manage = admin + member->admin
}
definition test/folder {
    relation viewer: auth/user | test/group#member | auth/user with gate with expiration
    relation project: test/project
    relation banned: auth/user
    relation approved: auth/user
    relation public: auth/user // rebac:const=alice
    permission view = viewer + project->access
    permission safe = view - banned
    permission both = view & approved
    permission open = public
}
definition test/project {
    relation folder: test/folder
    relation member: auth/user
    permission access = member + folder->view
}
definition test/doc {
    relation folder: test/folder | test/folder with gate with expiration
    relation viewer: auth/user
    permission read = folder->view + viewer
    permission safe = folder->safe
    permission both = folder->both
    permission open = folder->open
}
"""


FAST_DIFFERENTIAL_SCHEMA = """
definition auth/user {}
definition test/group {
    relation member: auth/user | test/group#member
}
definition test/doc {
    relation viewer: auth/user | test/group#member
    permission read = viewer
}
"""


def fast_differential_pool(length: int) -> list[RelationshipTuple]:
    names = [f"g{n}" for n in range(length)]
    return [
        *(
            row(
                "test/group",
                name,
                "member",
                SubjectRef.of("test/group", names[(n + 1) % length], "member"),
            )
            for n, name in enumerate(names)
        ),
        row("test/group", names[0], "member"),
        row("test/group", names[1], "member", SubjectRef.of("auth/user", "bob")),
        row("test/doc", "d", "viewer", SubjectRef.of("test/group", names[-1], "member")),
    ]


def differential_pool(length: int) -> list[RelationshipTuple]:
    people = [SubjectRef.of("auth/user", name) for name in ("alice", "bob")]
    groups = [f"g{n}" for n in range(length)]
    result = [
        row(
            "test/group",
            name,
            "member",
            SubjectRef.of("test/group", groups[(n + 1) % length], "member"),
        )
        for n, name in enumerate(groups)
    ]
    for group in groups:
        result.extend(row("test/group", group, "member", person) for person in people)
    for name in ("f0", "f1"):
        for person in people:
            result.extend(
                row("test/folder", name, relation, person)
                for relation in ("viewer", "banned", "approved")
            )
        result.append(
            row("test/folder", name, "viewer", SubjectRef.of("test/group", groups[0], "member"))
        )
        result.append(row("test/folder", name, "project", SubjectRef.of("test/project", "p0")))
    result.extend(
        [
            row("test/project", "p0", "folder", SubjectRef.of("test/folder", "f0")),
            row("test/project", "p0", "member"),
            row("test/team", "t0", "member", SubjectRef.of("test/team", "t1", "member")),
            row("test/team", "t1", "member", SubjectRef.of("test/team", "t0", "member")),
            row("test/team", "t0", "admin"),
            row("test/doc", "d0", "folder", SubjectRef.of("test/folder", "f0")),
            row("test/doc", "d0", "folder", SubjectRef.of("test/folder", "f1"), caveat_name="gate"),
            row("test/doc", "d0", "viewer"),
        ]
    )
    expiry = timezone.now() + timedelta(days=3)
    result.append(row("test/folder", "f0", "viewer", caveat_name="gate", expires_at=expiry))
    result.append(
        row("test/doc", "d1", "folder", SubjectRef.of("test/folder", "f1"), expires_at=expiry)
    )
    return result


# ``open`` is left out: its constant relation needs a model, and ``test/folder`` has none.
# ``test/folder#view`` and ``test/project#access`` recurse through each other.
# Over a data cycle a check that is not granted within the depth bound raises
# (pinned in test_compile_regressions), and so does a document that reads them.
MUTUALLY_RECURSIVE = ("test/folder", "test/project", "test/doc")

DIFFERENTIAL_PROBES = [
    *((ObjectRef("test/doc", name), ["read", "safe", "both"]) for name in ("d0", "d1")),
    *((ObjectRef("test/folder", name), ["view", "safe", "both"]) for name in ("f0", "f1")),
    (ObjectRef("test/project", "p0"), ["access"]),
    *((ObjectRef("test/team", name), ["manage"]) for name in ("t0", "t1")),
]


@pytest.mark.parametrize(
    "seed,steps",
    [
        (0, 8),
        (1, 8),
        (2, 8),
        (3, 8),
        pytest.param(0, 40, marks=pytest.mark.slow),
        pytest.param(1, 40, marks=pytest.mark.slow),
        pytest.param(2, 40, marks=pytest.mark.slow),
        pytest.param(3, 40, marks=pytest.mark.slow),
        pytest.param(10, 300, marks=pytest.mark.slow),
        pytest.param(11, 300, marks=pytest.mark.slow),
        pytest.param(12, 300, marks=pytest.mark.slow),
        pytest.param(13, 300, marks=pytest.mark.slow),
    ],
)
def test_seeded_randomized_change_sequences_match_reference(seed: int, steps: int):
    rng = random.Random(seed)
    wide = steps > 40
    local = install_schema(DIFFERENTIAL_SCHEMA if wide else FAST_DIFFERENTIAL_SCHEMA)
    candidates = differential_pool(2 + seed % 4) if wide else fast_differential_pool(2 + seed % 4)
    subjects = [USER, BOB, SubjectRef.of("auth/user", "nobody")]
    probes = DIFFERENTIAL_PROBES if wide else [(ObjectRef("test/doc", "d"), ["read"])]
    # The probe has its own generator, so it does not alter the sequence of changes.
    probe = random.Random(seed)
    present: set[int] = set()
    for step in range(steps):
        choices = rng.sample(range(len(candidates)), rng.choice((1, 1, 2, 3)))
        if present and step % 3 == 0:
            choices[0] = rng.choice(sorted(present))
        with transaction.atomic():
            for index in choices:
                if index in present and rng.random() < 0.7:
                    local.delete_relationship(candidates[index])
                    present.discard(index)
                else:
                    local.write_relationships([candidates[index]])
                    present.add(index)
        assert not local.check_access(
            subject=SubjectRef.of("auth/user", "nobody"),
            resource=ObjectRef("test/team", "t0"),
            action="manage",
        ).allowed
        resource, actions = probe.choice(probes)
        assert_reads_match(
            subjects=subjects,
            resources=[resource],
            actions=actions,
            contexts=[{"ok": True}],
            undecided=MUTUALLY_RECURSIVE,
        )
    for resource, actions in probes:
        assert_reads_match(
            subjects=subjects,
            resources=[resource],
            actions=actions,
            contexts=[None, {"ok": True}, {"ok": False}] if wide else [None],
            undecided=MUTUALLY_RECURSIVE,
        )


REVOKE_SCHEMA = """
definition auth/user {}
definition test/group {
    relation member: auth/user | auth/user:* | test/group#member
}
definition test/folder {
    relation parent: test/folder
    relation viewer: auth/user | auth/user:* | test/group#member
    relation banned: auth/user | test/group#member
    relation approved: auth/user | test/group#member
    permission read = viewer + parent->read
    permission safe = read - banned
    permission both = read & approved
}
definition test/doc {
    relation folder: test/folder
    relation banned: auth/user
    relation approved: auth/user
    permission read = folder->read
    permission safe = folder->safe
    permission both = folder->both
    permission mixed = folder->read - banned
    permission inter = folder->read & approved
}
"""


def revoke_chain(depth: int) -> list[RelationshipTuple]:
    rows = [row("test/folder", "f0", "viewer")]
    rows.extend(
        row("test/folder", f"f{n}", "parent", SubjectRef.of("test/folder", f"f{n - 1}"))
        for n in range(1, depth + 1)
    )
    rows.append(row("test/doc", "d", "folder", SubjectRef.of("test/folder", f"f{depth}")))
    return rows


def revocation_cases():
    folder = row("test/folder", "f", "viewer")
    doc = row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f"))
    group = row("test/group", "g", "member")
    group_view = row("test/folder", "f", "viewer", SubjectRef.of("test/group", "g", "member"))
    wildcard = SubjectRef.of("auth/user", "*")
    return [
        pytest.param([folder, doc], folder, ("test/doc", "d", "read"), id="direct-grant"),
        pytest.param([group, group_view, doc], group, ("test/doc", "d", "read"), id="membership"),
        pytest.param(
            [
                row("test/group", "g2", "member"),
                row("test/group", "g", "member", SubjectRef.of("test/group", "g2", "member")),
                group_view,
                doc,
            ],
            row("test/group", "g", "member", SubjectRef.of("test/group", "g2", "member")),
            ("test/doc", "d", "read"),
            id="nested-membership",
        ),
        pytest.param(
            [row("test/folder", "f", "viewer", wildcard), doc],
            row("test/folder", "f", "viewer", wildcard),
            ("test/doc", "d", "read"),
            id="wildcard-viewer",
        ),
        pytest.param(
            [row("test/group", "g", "member", wildcard), group_view, doc],
            row("test/group", "g", "member", wildcard),
            ("test/doc", "d", "read"),
            id="wildcard-member",
        ),
        pytest.param(
            [folder, row("test/folder", "f", "approved"), doc, row("test/doc", "d", "approved")],
            row("test/folder", "f", "approved"),
            ("test/doc", "d", "both"),
            id="intersection-operand",
        ),
        pytest.param(
            [folder, doc, row("test/doc", "d", "approved")],
            row("test/doc", "d", "approved"),
            ("test/doc", "d", "inter"),
            id="doc-intersection-operand",
        ),
        pytest.param(
            [folder, doc],
            row("test/folder", "f", "banned"),
            ("test/doc", "d", "safe"),
            id="ban-grows",
        ),
        pytest.param(
            [
                folder,
                row("test/folder", "f", "banned", SubjectRef.of("test/group", "g", "member")),
                doc,
            ],
            row("test/group", "g", "member"),
            ("test/doc", "d", "safe"),
            id="ban-grows-by-membership",
        ),
        pytest.param(
            [folder, doc],
            row("test/doc", "d", "banned"),
            ("test/doc", "d", "mixed"),
            id="doc-ban-grows",
        ),
        pytest.param(
            [folder, doc, row("test/doc", "d", "approved")],
            folder,
            ("test/doc", "d", "inter"),
            id="mixed-arrow-operand",
        ),
        *(
            pytest.param(
                revoke_chain(depth),
                revoke_chain(depth)[0],
                ("test/doc", "d", "read"),
                id=f"chain-{depth}",
            )
            for depth in (1, 3, 8)
        ),
        pytest.param(
            revoke_chain(8), revoke_chain(8)[4], ("test/doc", "d", "read"), id="chain-middle"
        ),
        pytest.param(
            [
                row("test/folder", "a", "viewer"),
                row("test/folder", "a", "parent", SubjectRef.of("test/folder", "c")),
                row("test/folder", "b", "parent", SubjectRef.of("test/folder", "a")),
                row("test/folder", "c", "parent", SubjectRef.of("test/folder", "b")),
                row("test/doc", "d", "folder", SubjectRef.of("test/folder", "c")),
            ],
            row("test/folder", "a", "viewer"),
            ("test/doc", "d", "read"),
            id="recursive-cycle-three",
        ),
        pytest.param(
            [
                row("test/folder", "root", "viewer"),
                row("test/folder", "a", "parent", SubjectRef.of("test/folder", "root")),
                row("test/folder", "a", "parent", SubjectRef.of("test/folder", "c")),
                row("test/folder", "b", "parent", SubjectRef.of("test/folder", "a")),
                row("test/folder", "c", "parent", SubjectRef.of("test/folder", "b")),
                row("test/doc", "d", "folder", SubjectRef.of("test/folder", "c")),
            ],
            row("test/folder", "a", "parent", SubjectRef.of("test/folder", "root")),
            ("test/doc", "d", "read"),
            id="recursive-cycle-break-link",
        ),
        pytest.param([folder, doc], doc, ("test/doc", "d", "read"), id="unlink-doc"),
    ]


@pytest.mark.parametrize("initial,changed,check", revocation_cases())
def test_revocation_operand_reaches_all_readers(initial, changed, check):
    local = install_schema(REVOKE_SCHEMA)
    local.write_relationships(initial)
    resource_type, resource_id, action = check
    assert local.check_access(
        subject=USER, resource=ObjectRef(resource_type, resource_id), action=action
    ).allowed
    if changed in initial:
        local.delete_relationship(changed)
    else:
        local.write_relationships([changed])
    assert not local.check_access(
        subject=USER, resource=ObjectRef(resource_type, resource_id), action=action
    ).allowed


PAYLOAD_SCHEMA = """
caveat gate(ok bool) { ok }
definition auth/user {}
definition test/group {
    relation member: auth/user | test/group#member | auth/user with expiration
}
definition test/folder {
    relation parent: test/folder | test/folder with gate | test/folder with expiration
    relation viewer: auth/user | auth/user with gate | test/group#member | auth/user with expiration
    relation follower: auth/user
    permission read = viewer + parent->read
    permission p1 = follower
    permission p2 = p1
}
definition test/doc {
    relation folder: test/folder | test/folder with gate | test/folder with expiration
    permission read = folder->read
    permission follow = folder->p2
}
"""


def payload_chain(depth: int) -> list[RelationshipTuple]:
    rows = [row("test/folder", "f0", "viewer")]
    rows.extend(
        row("test/folder", f"f{n}", "parent", SubjectRef.of("test/folder", f"f{n - 1}"))
        for n in range(1, depth + 1)
    )
    rows.append(row("test/doc", "d", "folder", SubjectRef.of("test/folder", f"f{depth}")))
    return rows


def test_root_caveat_replacement_revokes_depth_eight():
    local = install_schema(PAYLOAD_SCHEMA)
    chain = payload_chain(8)
    local.write_relationships(chain)
    with transaction.atomic():
        local.delete_relationship(chain[0])
        local.write_relationships([row("test/folder", "f0", "viewer", caveat_name="gate")])
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="read", context={"ok": False}
    ).allowed


def test_root_expiry_payload_change_reaches_depth_eight():
    local = install_schema(PAYLOAD_SCHEMA)
    soon = timezone.now() + timedelta(minutes=1)
    later = soon + timedelta(days=2)
    chain = payload_chain(8)
    chain[0] = row("test/folder", "f0", "viewer", expires_at=soon)
    local.write_relationships(chain)
    local.write_relationships([row("test/folder", "f0", "viewer", expires_at=later)])
    document = ObjectRef("test/doc", "d")
    assert local.check_access(subject=USER, resource=document, action="read").allowed
    assert read_at(local, document, "read", soon + timedelta(hours=1)).allowed


@pytest.mark.parametrize("relation", ["folder", "parent"])
def test_arrow_via_edge_caveat_replacement_revokes(relation):
    local = install_schema(PAYLOAD_SCHEMA)
    chain = payload_chain(2)
    old = chain[-1] if relation == "folder" else chain[2]
    local.write_relationships(chain)
    replacement = row(
        old.resource.resource_type,
        old.resource.resource_id,
        old.relation,
        old.subject,
        caveat_name="gate",
    )
    with transaction.atomic():
        local.delete_relationship(old)
        local.write_relationships([replacement])
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="read", context={"ok": False}
    ).allowed


def test_arrow_via_edge_expiry_payload_shortening_reaches_reader():
    local = install_schema(PAYLOAD_SCHEMA)
    soon = timezone.now() + timedelta(minutes=1)
    later = soon + timedelta(days=2)
    local.write_relationships(
        [
            row("test/folder", "f", "viewer"),
            row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f"), expires_at=later),
        ]
    )
    local.write_relationships(
        [row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f"), expires_at=soon)]
    )
    document = ObjectRef("test/doc", "d")
    assert local.check_access(subject=USER, resource=document, action="read").allowed
    assert not read_at(local, document, "read", soon + timedelta(hours=1)).allowed


def test_membership_expiry_payload_change_reaches_container():
    local = install_schema(PAYLOAD_SCHEMA)
    soon = timezone.now() + timedelta(minutes=1)
    later = soon + timedelta(days=1)
    local.write_relationships(
        [
            row("test/group", "g", "member", expires_at=soon),
            row("test/group", "outer", "member", SubjectRef.of("test/group", "g", "member")),
            row("test/folder", "f", "viewer", SubjectRef.of("test/group", "outer", "member")),
            row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f")),
        ]
    )
    local.write_relationships([row("test/group", "g", "member", expires_at=later)])
    document = ObjectRef("test/doc", "d")
    assert local.check_access(subject=USER, resource=document, action="read").allowed
    assert read_at(local, document, "read", soon + timedelta(hours=1)).allowed


def test_same_scope_grant_change_reaches_arrow_reader():
    local = install_schema(PAYLOAD_SCHEMA)
    local.write_relationships([row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f"))])
    local.write_relationships([row("test/folder", "f", "follower")])
    local.delete_relationship(row("test/folder", "f", "follower"))
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="follow"
    ).allowed


@pytest.mark.parametrize("order", ["delete_first", "write_first"])
def test_tuple_remove_and_readd_in_one_transaction_reads_final_tuples(order):
    local = install_schema(PAYLOAD_SCHEMA)
    chain = payload_chain(3)
    local.write_relationships(chain)
    with transaction.atomic():
        if order == "delete_first":
            local.delete_relationship(chain[0])
            local.write_relationships([chain[0]])
        else:
            local.write_relationships([chain[0]])
            local.delete_relationship(chain[0])
    assert local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="read"
    ).allowed is (order == "delete_first")


def test_tuple_grant_and_revoke_related_tuples_in_one_transaction():
    local = install_schema(PAYLOAD_SCHEMA)
    bob = SubjectRef.of("auth/user", "bob")
    chain = payload_chain(3)
    local.write_relationships(chain)
    with transaction.atomic():
        local.write_relationships([row("test/folder", "f2", "viewer", bob)])
        local.delete_relationship(chain[0])
        local.write_relationships([row("test/folder", "x", "viewer")])
        local.delete_relationship(chain[2])
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="read"
    ).allowed
    assert local.check_access(
        subject=bob, resource=ObjectRef("test/doc", "d"), action="read"
    ).allowed


LARGE_FAN_OUT_SCHEMA = """
definition auth/user {}
definition test/thread {
    relation viewer: auth/user
    permission read = viewer
}
definition test/message {
    relation thread: test/thread
    permission read = thread->read
}
"""


@pytest.mark.slow
def test_thirty_three_thousand_arrow_readers_see_a_new_grant():
    local = install_schema(LARGE_FAN_OUT_SCHEMA)
    local.write_relationships(
        [
            row("test/message", str(n), "thread", SubjectRef.of("test/thread", "root"))
            for n in range(33_000)
        ]
    )
    local.write_relationships([row("test/thread", "root", "viewer")])
    assert local.check_access(
        subject=USER, resource=ObjectRef("test/message", "32999"), action="read"
    ).allowed


@pytest.mark.slow
def test_thirty_three_thousand_bulk_created_tuples_see_a_new_grant():
    local = install_schema(LARGE_FAN_OUT_SCHEMA)
    Relationship.objects.bulk_create(
        [
            Relationship(
                resource_type="test/message",
                resource_id=str(n),
                relation="thread",
                subject_type="test/thread",
                subject_id="root",
                optional_subject_relation="",
            )
            for n in range(33_000)
        ],
        batch_size=256,
    )
    local.write_relationships([row("test/thread", "root", "viewer")])
    assert local.check_access(
        subject=USER, resource=ObjectRef("test/message", "32999"), action="read"
    ).allowed


MODEL_SCALE_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
"""


@pytest.mark.slow
def test_thirty_three_thousand_bulk_created_rows_read_through_their_folder():
    local = install_schema(MODEL_SCALE_SCHEMA)
    with sudo(reason="write propagation scale test"):
        folder = Folder.objects.create(name="root")
        local.write_relationships([row("blog/folder", str(folder.pk), "viewer")])
        posts = Post.objects.bulk_create(
            [Post(title=str(n), folder=folder) for n in range(33_000)], batch_size=500
        )
    assert local.check_access(
        subject=USER, resource=to_object_ref(posts[-1]), action="read"
    ).allowed


@pytest.fixture
def persisted(db):
    """A stored policy with one grant; returns its ``read`` permission row."""
    reset_backend()
    with schema_changes():
        SchemaDefinition.objects.create(resource_type="auth/user")
        definition = SchemaDefinition.objects.create(resource_type="test/policy")
        SchemaRelation.objects.create(
            definition=definition, name="viewer", allowed_subjects=[{"type": "auth/user"}]
        )
        permission = SchemaPermission.objects.create(
            definition=definition, name="read", expression="viewer"
        )
        SchemaPermission.objects.create(definition=definition, name="dependent", expression="read")
    backend().write_relationships([row("test/policy", "one", "viewer")])
    return permission


@pytest.mark.parametrize("order", ["first", "middle", "last"])
def test_nested_override_and_interleaved_grant_revoke_read_final_policy(persisted, order):
    local = backend()

    def disable_read():
        return SchemaOverride.objects.create(
            kind="disable",
            target_ct=ContentType.objects.get_for_model(SchemaPermission),
            target_pk=persisted.pk,
            expression="viewer",
            reason="propagation test",
        )

    with transaction.atomic():
        if order == "first":
            disable_read()
        local.write_relationships(
            [row("test/policy", "two", "viewer", SubjectRef.of("auth/user", "bob"))]
        )
        if order == "middle":
            disable_read()
        local.delete_relationship(row("test/policy", "one", "viewer"))
        local.write_relationships([row("test/policy", "three", "viewer")])
        if order == "last":
            disable_read()
    assert all(
        not local.check_access(
            subject=subject, resource=ObjectRef("test/policy", name), action="read"
        ).allowed
        for subject, name in (
            (USER, "one"),
            (SubjectRef.of("auth/user", "bob"), "two"),
            (USER, "three"),
        )
    )


def test_nested_override_create_then_delete_keeps_tuple_revoke(persisted):
    local = backend()
    bob = SubjectRef.of("auth/user", "bob")
    with transaction.atomic():
        override = SchemaOverride.objects.create(
            kind="disable",
            target_ct=ContentType.objects.get_for_model(SchemaPermission),
            target_pk=persisted.pk,
            expression="viewer",
            reason="propagation test",
        )
        local.write_relationships([row("test/policy", "two", "viewer", bob)])
        override.delete()
        local.delete_relationship(row("test/policy", "one", "viewer"))
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/policy", "one"), action="read"
    ).allowed
    assert local.check_access(
        subject=bob, resource=ObjectRef("test/policy", "two"), action="dependent"
    ).allowed


MODEL_PROPAGATION_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    relation parent: blog/folder // rebac:field=parent
    permission read = viewer + parent->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation collections: blog/folder // rebac:field=collections
    permission read = folder->read + collections->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
"""


def test_nested_field_reparent_a_b_a_reads_final_folder():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    bob = SubjectRef.of("auth/user", "bob")
    with sudo(reason="write propagation test"):
        a, b = Folder.objects.create(name="a"), Folder.objects.create(name="b")
        post = Post.objects.create(title="p", folder=a)
        local.write_relationships([row("blog/folder", str(a.pk), "viewer")])
        local.write_relationships([row("blog/folder", str(b.pk), "viewer", bob)])
        with transaction.atomic():
            post.folder = b
            post.save(update_fields=["folder"])
            post.folder = a
            post.save(update_fields=["folder"])
    assert local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
    assert not local.check_access(subject=bob, resource=to_object_ref(post), action="read").allowed


@pytest.mark.slow
def test_nested_field_reparent_with_interleaved_grant_and_revoke():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    bob = SubjectRef.of("auth/user", "bob")
    with sudo(reason="write propagation test"):
        a, b = Folder.objects.create(name="a"), Folder.objects.create(name="b")
        post = Post.objects.create(title="p", folder=a)
        local.write_relationships([row("blog/folder", str(a.pk), "viewer")])
        with transaction.atomic():
            post.folder = b
            post.save(update_fields=["folder"])
            local.write_relationships([row("blog/folder", str(b.pk), "viewer", bob)])
            local.delete_relationship(row("blog/folder", str(a.pk), "viewer"))
            post.folder = a
            post.save(update_fields=["folder"])
            local.write_relationships([row("blog/folder", str(a.pk), "viewer", bob)])
    assert not local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
    assert local.check_access(subject=bob, resource=to_object_ref(post), action="read").allowed


@pytest.mark.slow
def test_duplicate_field_and_collection_edges_survive_each_other():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    with sudo(reason="write propagation test"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
        local.write_relationships([row("blog/folder", str(folder.pk), "viewer")])
        post.collections.add(folder)
        post.collections.remove(folder)
        assert local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
        post.collections.add(folder)
        post.folder = None
        post.save(update_fields=["folder"])
        assert local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
        post.collections.clear()
    assert not local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed


def test_nested_savepoint_rollback_discards_inner_writes():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    bob = SubjectRef.of("auth/user", "bob")
    with sudo(reason="write propagation test"):
        a, b = Folder.objects.create(name="a"), Folder.objects.create(name="b")
        post = Post.objects.create(title="p", folder=a)
        local.write_relationships([row("blog/folder", str(a.pk), "viewer")])
        with transaction.atomic():
            try:
                with transaction.atomic():
                    post.folder = b
                    post.save(update_fields=["folder"])
                    local.write_relationships([row("blog/folder", str(b.pk), "viewer", bob)])
                    raise RuntimeError("rollback")
            except RuntimeError:
                pass
            post.refresh_from_db()
            local.write_relationships([row("blog/folder", str(a.pk), "viewer", bob)])
    assert local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
    assert local.check_access(subject=bob, resource=to_object_ref(post), action="read").allowed


SET_OPERATION_SCHEMA = """
definition auth/user {}
definition test/folder {
    relation viewer: auth/user | auth/user with expiration
    relation banned: auth/user
    relation parent: test/folder
    permission read = viewer + parent->read
    permission safe = read - banned
    permission open = authenticated - banned
    permission none = nil
    permission minus_nil = viewer - nil
    permission and_nil = viewer & nil
}
definition test/doc {
    relation folder: test/folder
    relation banned: auth/user
    relation editor: auth/user
    permission safe = folder->safe
    permission open = folder->open
    permission mix = (folder->read + editor) - banned
    permission mix2 = (folder->safe & editor) + folder->open
    permission nn = folder->none + folder->minus_nil + folder->and_nil
}
"""


@pytest.mark.slow
def test_exclusion_intersection_nil_and_type_level_sequence():
    local = install_schema(SET_OPERATION_SCHEMA)
    doc_folder = row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f"))
    local.write_relationships([doc_folder])
    steps = [
        ("write", row("test/folder", "f", "viewer")),
        ("write", row("test/doc", "d", "editor")),
        ("write", row("test/folder", "f", "banned")),
        ("write", row("test/doc", "d", "banned")),
        ("delete", row("test/folder", "f", "banned")),
        ("delete", row("test/doc", "d", "editor")),
        ("delete", row("test/doc", "d", "banned")),
        ("delete", row("test/folder", "f", "viewer")),
        ("write", row("test/folder", "root", "viewer")),
        ("write", row("test/folder", "f", "parent", SubjectRef.of("test/folder", "root"))),
        ("delete", row("test/folder", "f", "parent", SubjectRef.of("test/folder", "root"))),
    ]
    for operation, tuple_ in steps:
        if operation == "write":
            local.write_relationships([tuple_])
        else:
            local.delete_relationship(tuple_)
        assert_reads_match(
            subjects=[USER, BOB],
            resources=[ObjectRef("test/folder", name) for name in ("f", "root")],
            actions=["read", "safe", "open", "none", "minus_nil", "and_nil"],
        )
        assert_reads_match(
            subjects=[USER, BOB],
            resources=[ObjectRef("test/doc", "d")],
            actions=["safe", "open", "mix", "mix2", "nn"],
        )
    assert local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="open"
    ).allowed
    assert not local.check_access(
        subject=USER, resource=ObjectRef("test/doc", "d"), action="nn"
    ).allowed


def test_exclusion_left_operand_expiry_change_reaches_arrow_reader():
    local = install_schema(SET_OPERATION_SCHEMA)
    soon = timezone.now() + timedelta(minutes=1)
    later = soon + timedelta(days=2)
    local.write_relationships(
        [
            row("test/folder", "f", "viewer", expires_at=soon),
            row("test/doc", "d", "folder", SubjectRef.of("test/folder", "f")),
        ]
    )
    local.write_relationships([row("test/folder", "f", "viewer", expires_at=later)])
    local.write_relationships(
        [row("test/folder", "f", "viewer", SubjectRef.of("auth/user", "bob"))]
    )
    document = ObjectRef("test/doc", "d")
    assert local.check_access(subject=USER, resource=document, action="mix").allowed
    assert read_at(local, document, "mix", soon + timedelta(hours=1)).allowed
    assert not read_at(local, document, "mix", later).allowed


@pytest.mark.slow
def test_model_bulk_update_delete_and_cascade_revoke_access():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    with sudo(reason="write propagation test"):
        left, right = Folder.objects.create(name="left"), Folder.objects.create(name="right")
        local.write_relationships([row("blog/folder", str(left.pk), "viewer")])
        posts = Post.objects.bulk_create(
            [Post(title=str(n), folder=left) for n in range(30)], batch_size=10
        )
        for post in posts[:10]:
            post.folder = right
        Post.objects.bulk_update(posts[:10], ["folder"], batch_size=3)
        Post.objects.filter(pk__in=[post.pk for post in posts[10:20]]).update(folder=right)
        Post.objects.filter(pk__in=[post.pk for post in posts[20:]]).delete()
        left.delete()
    assert not any(
        local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
        for post in posts[:20]
    )


def test_model_cascade_delete_removes_recursive_field_grants():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    with sudo(reason="write propagation test"):
        root = Folder.objects.create(name="root")
        mid = Folder.objects.create(name="mid", parent=root)
        leaf = Folder.objects.create(name="leaf", parent=mid)
        post = Post.objects.create(title="p", folder=leaf)
        local.write_relationships([row("blog/folder", str(root.pk), "viewer")])
        mid.delete()
        post.refresh_from_db()
    assert not local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed


@pytest.mark.slow
def test_model_reverse_m2m_remove_set_and_clear_revoke_access():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    with sudo(reason="write propagation test"):
        folder, other = Folder.objects.create(name="f"), Folder.objects.create(name="other")
        local.write_relationships([row("blog/folder", str(folder.pk), "viewer")])
        posts = [Post.objects.create(title=str(n)) for n in range(4)]
        folder.collected_posts.add(*posts)
        folder.collected_posts.remove(posts[0])
        posts[1].collections.set([other])
        folder.collected_posts.clear()
    assert not any(
        local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
        for post in posts
    )


def test_model_reader_keeps_grant_when_nested_revocation_rolls_back():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    with sudo(reason="write propagation test"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
        grant = row("blog/folder", str(folder.pk), "viewer")
        local.write_relationships([grant])
        with transaction.atomic():
            with pytest.raises(RuntimeError):
                with transaction.atomic():
                    local.delete_relationship(grant)
                    raise RuntimeError("mid write")
        assert local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
        local.delete_relationship(grant)
    assert not local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed


MODEL_FILTER_SCHEMA = """
definition auth/user {}
definition site/audience { permission read = authenticated }
definition blog/folder {
    relation viewer: auth/user
    relation parent: blog/folder // rebac:field=parent
    relation public: site/audience // rebac:const={"target_id":"public","filters":{"is_active":true}}
    permission read = viewer + parent->read
    permission open = public->read
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
    permission open = folder->open
    permission create = authenticated
    permission write = authenticated
    permission delete = authenticated
}
"""


@pytest.mark.slow
def test_model_field_chain_reparent_and_const_filter_transition():
    local = install_schema(MODEL_FILTER_SCHEMA)
    with sudo(reason="write propagation test"):
        root = Folder.objects.create(name="root")
        child = Folder.objects.create(name="child", parent=root)
        leaf = Folder.objects.create(name="leaf", parent=child)
        post = Post.objects.create(title="p", folder=leaf)
        local.write_relationships([row("blog/folder", str(root.pk), "viewer")])
        child.parent = None
        child.save(update_fields=["parent"])
        assert not local.check_access(
            subject=USER, resource=to_object_ref(post), action="read"
        ).allowed
        leaf.is_active = False
        leaf.save(update_fields=["is_active"])
        assert not local.check_access(
            subject=USER, resource=to_object_ref(post), action="open"
        ).allowed
        Folder.objects.filter(pk=leaf.pk).update(is_active=True)
    assert local.check_access(subject=USER, resource=to_object_ref(post), action="open").allowed


@pytest.mark.slow
def test_bulk_tuple_write_then_queryset_delete_revokes_field_reader():
    local = install_schema(MODEL_PROPAGATION_SCHEMA)
    with sudo(reason="write propagation test"):
        folder = Folder.objects.create(name="f")
        post = Post.objects.create(title="p", folder=folder)
        local.write_relationships(
            [
                *(
                    row(
                        "blog/folder", str(folder.pk), "viewer", SubjectRef.of("auth/user", f"u{n}")
                    )
                    for n in range(520)
                ),
                row("blog/folder", str(folder.pk), "viewer"),
            ]
        )
        active_relationship_model().objects.filter(
            resource_type="blog/folder", resource_id=str(folder.pk)
        ).delete()
    assert not local.check_access(subject=USER, resource=to_object_ref(post), action="read").allowed
