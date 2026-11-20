"""``REBAC_INHERIT_THROUGH_LINKS`` (proposal 0023): a policy evaluated without inheritance through links.

The classification of relations; the transformation on small policies; what is
kept and what is lost, on rows; generated names, which no policy can declare and
no public entry accepts as an action; the caches keyed by the setting; a
narrowing that cannot be built, which fails reads closed and leaves writes
alone; the cost of narrowing; overrides; the invariant (the narrowed policy
grants a subset of what the published one grants) on two families of random
policies over every read path; the report, the commands and the system checks.
The transformation alone is fuzzed against a set-semantics reference in
``tests/test_inherit_through_links_fuzz.py``.
"""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from django.db import connection
from django.db.models import F, Prefetch
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from rebac import (
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    check_new,
    check_permission,
    current_evaluator,
    evaluator_scope,
    links,
    permission_expression,
    require_permission,
    rowscope,
    sudo,
)
from rebac.backends import backend, reset_backend
from rebac.compile.program import CompileProgram
from rebac.conf import app_settings
from rebac.errors import SchemaError
from rebac.schema.ast import (
    AllowedSubject,
    Definition,
    PermArrow,
    PermBinOp,
    Permission,
    PermRef,
    Relation,
    Schema,
    backing_to_dict,
)
from rebac.schema.parser import ParseError, parse_zed
from rebac.schema.rendering import _render_expr, render_zed
from rebac.schema.walker import field_gated_actions
from rebac.testing import install_schema
from rebac.types import CheckItem, RelationshipFilter
from tests.testapp.models import (
    Attachment,
    Folder,
    NativeParentLinkedBranch,
    NativeParentLinkedChild,
    NativeParentLinkedNote,
    NativeParentLinkedResource,
    Post,
    ScopeMessage,
    ScopePart,
    ScopeProfile,
    ScopeThread,
    Shelf,
)

MEMBER = SubjectRef.of("auth/user", "member")
ADMIN = SubjectRef.of("auth/user", "admin")
EVERYONE = SubjectRef.of("auth/user", "*")
SETTING = "REBAC_INHERIT_THROUGH_LINKS"


def grant(
    type_: str, id_: object, relation: str, subject: SubjectRef = MEMBER
) -> RelationshipTuple:
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)


def expression(schema: Schema, type_: str, name: str) -> str:
    permission = schema.get_permission(type_, name)
    assert permission is not None, (type_, name)
    return _render_expr(permission.expression)


def flip(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    """Change the setting as a running process sees it, with no signal and no reset."""
    monkeypatch.setitem(app_settings._cache, SETTING, value)


# ---------- Kinds of relation ----------

CLASSIFIED = """
definition auth/user {}
definition scope/role { relation member: auth/user }
definition test/roleanchor { relation member: auth/user }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation squad: scope/role
    relation anchor: test/roleanchor
    relation sibling: scope/thread
    relation messages: scope/message // rebac:field=scopemessage
    relation pinned: scope/message
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation profile: scope/profile // rebac:field=profile
}
definition scope/profile {
    relation owner: scope/message // rebac:field=owner
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    relation first: scope/message // rebac:field={"path":"message","filters":{"fragment_id":1}}
    relation thread: scope/thread // rebac:field=message__thread
    relation lead: scope/message // rebac:field={"path":"message","filters":{"message__thread__label":"hidden"}}
    relation missing: scope/message // rebac:field=nothing
}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation grandparent: blog/folder // rebac:field=parent__parent
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation collections: blog/folder // rebac:field=collections
}
definition blog/linkedpost {
    relation peers: blog/linkedpost // rebac:field=peers
}
definition blog/sluggedpost {
    relation member: blog/folder // rebac:attribute={"field":"name"}
}
definition test/nativeparentlinkedresource {}
definition test/nativeparentlinkedchild {
    relation resource: test/nativeparentlinkedresource // rebac:field=nativeparentlinkedresource_ptr
    relation owner: auth/user // rebac:field=owner
}
definition test/shelf {
    relation attachments: test/attachment // rebac:field=attachments
}
definition test/attachment {
    relation target: test/shelf // rebac:field=target
}
"""


@pytest.mark.parametrize(
    ("type_", "relation", "kind", "reason", "parent_link"),
    [
        ("scope/message", "thread", links.PARENT, "a foreign key of the row", False),
        ("scope/profile", "owner", links.PARENT, "a foreign key of the row", False),
        ("scope/part", "first", links.PARENT, "a foreign key of the row", False),
        (
            "test/nativeparentlinkedchild",
            "resource",
            links.PARENT,
            "the multi-table parent link",
            True,
        ),
        ("test/nativeparentlinkedchild", "owner", links.PARENT, "a foreign key of the row", False),
        ("scope/thread", "admin", links.CONSTANT, "a constant relation", False),
        ("scope/thread", "sibling", links.SAME_TYPE, "records of its own type", False),
        ("blog/folder", "parent", links.PARENT, "a foreign key of the row", False),
        # A walk within one type is kept whole whatever its backing.
        ("blog/folder", "grandparent", links.SAME_TYPE, "records of its own type", False),
        ("blog/linkedpost", "peers", links.SAME_TYPE, "records of its own type", False),
        ("scope/thread", "squad", links.MEMBERSHIP, "subjects that store no rows", False),
        ("scope/thread", "anchor", links.MEMBERSHIP, "subjects that store no rows", False),
        ("scope/part", "thread", links.LINK, "a path of several hops", False),
        ("scope/part", "lead", links.LINK, "a filter over another table", False),
        ("scope/part", "missing", links.LINK, "a backing that does not resolve", False),
        ("scope/message", "profile", links.LINK, "a reverse relation", False),
        ("scope/thread", "messages", links.LINK, "a reverse relation", False),
        (
            "scope/thread",
            "pinned",
            links.LINK,
            "a stored relation to records of another type",
            False,
        ),
        ("blog/post", "collections", links.LINK, "a many-to-many relation", False),
        ("blog/sluggedpost", "member", links.LINK, "an attribute backing", False),
        ("test/attachment", "target", links.LINK, "a generic relation", False),
        ("test/shelf", "attachments", links.LINK, "a generic relation", False),
    ],
)
def test_each_relation_is_classified_from_its_resolved_backing(
    type_, relation, kind, reason, parent_link
):
    schema = parse_zed(CLASSIFIED)
    definition = schema.get_definition(type_)
    assert definition is not None
    found = links.classify(definition, next(r for r in definition.relations if r.name == relation))
    assert (found.kind, found.reason, found.parent_link) == (kind, reason, parent_link)


# ---------- Generated names ----------


def test_generated_names_are_names_no_policy_can_declare():
    for lent, negative, name in ((True, False, "read"), (False, True, "hold"), (True, True, "x")):
        generated = links.generated_name(lent, negative, name)
        assert links.is_generated(generated)
        assert links.generated_parts(generated) == (lent, negative, name)
        with pytest.raises(ParseError):
            parse_zed(f"definition t/a {{ relation r: t/a\n permission {generated} = r }}")
    assert links.generated_name(False, False, "read") == "read"
    assert links.generated_parts("read") is None
    assert not links.is_generated("lent__read")


# ---------- The transformation ----------

SMALL = """
definition auth/user {}
definition scope/role { relation member: auth/user }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation squad: scope/role
    relation sibling: scope/thread
    relation reader: auth/user
    relation blocked: auth/user
    permission read = reader + squad->member + admin->member + sibling->read
    permission veto = blocked
}
definition scope/profile {
    relation thread: scope/thread // rebac:field=owner__thread
    relation direct: auth/user
    relation banned: auth/user
    permission read = direct + thread->read
    permission screened = (direct + thread->read) - thread->veto
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation profile: scope/profile // rebac:field=profile
    relation direct: auth/user
    relation banned: auth/user
    permission read = thread->read + profile->read + direct
    permission guarded = direct - profile->read
    permission twice = direct - (banned - profile->read)
    permission both = direct & profile->read
    permission hold = banned + profile->read
    permission kept = direct - hold
    permission held = hold - banned
    permission lens = profile->screened
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    relation thread: scope/thread // rebac:field=message__thread
    permission read = message->read
    permission via_thread = thread->read
}
"""


def test_a_link_lends_only_what_the_linked_record_grants_directly():
    narrowed = links.narrowed(parse_zed(SMALL))
    # The row's own foreign key is followed whole; the path of two hops lends.
    assert expression(narrowed, "scope/part", "read") == "message->read"
    assert expression(narrowed, "scope/part", "via_thread") == "thread->lent(read)"
    # A thread lends its own relations, its constant role and its membership,
    # not its hierarchy.
    assert expression(narrowed, "scope/thread", "lent(read)") == (
        "((reader + squad->member) + admin->member)"
    )
    # A record's own permission keeps its parent, constant, membership and same-type arrows.
    assert expression(narrowed, "scope/thread", "read") == expression(
        parse_zed(SMALL), "scope/thread", "read"
    )
    assert expression(narrowed, "scope/message", "read") == (
        "((thread->read + profile->lent(read)) + direct)"
    )
    assert expression(narrowed, "scope/profile", "lent(read)") == "direct"


def test_under_one_subtraction_nothing_is_narrowed():
    narrowed = links.narrowed(parse_zed(SMALL))
    # The excluded profile read is the published one, under a name of its own.
    assert expression(narrowed, "scope/message", "guarded") == "(direct - profile->excluded(read))"
    assert expression(narrowed, "scope/profile", "excluded(read)") == "(direct + thread->read)"
    # A named permission under an exclusion is evaluated whole: a version of its own.
    assert expression(narrowed, "scope/message", "hold") == "(banned + profile->lent(read))"
    assert expression(narrowed, "scope/message", "kept") == "(direct - excluded(hold))"
    assert expression(narrowed, "scope/message", "excluded(hold)") == (
        "(banned + profile->excluded(read))"
    )
    assert expression(narrowed, "scope/message", "held") == "(hold - banned)"
    # Inside what a record lends, an excluded arrow is kept as the published policy has it.
    assert expression(narrowed, "scope/profile", "lent(screened)") == "(direct - thread->veto)"


def test_under_two_subtractions_and_in_an_intersection_it_narrows():
    narrowed = links.narrowed(parse_zed(SMALL))
    assert expression(narrowed, "scope/message", "twice") == (
        "(direct - (banned - profile->lent(read)))"
    )
    assert expression(narrowed, "scope/message", "both") == "(direct & profile->lent(read))"


def test_a_permission_named_inside_two_exclusions_is_narrowed_there():
    schema = parse_zed(
        SMALL
        + """
definition test/doc {
    relation message: scope/message // rebac:field=nothing
    relation direct: auth/user
    relation banned: auth/user
    permission inner = banned - message->read
    permission outer = direct - inner
}
"""
    )
    narrowed = links.narrowed(schema)
    # In its own evaluation inner's arrow is under one exclusion: the published message read.
    assert expression(narrowed, "test/doc", "inner") == "(banned - message->excluded(read))"
    # Under outer's exclusion it is under two: a granting position again.
    assert expression(narrowed, "test/doc", "outer") == "(direct - excluded(inner))"
    assert expression(narrowed, "test/doc", "excluded(inner)") == "(banned - message->lent(read))"
    assert expression(narrowed, "scope/message", "lent(read)") == "direct"


PARENT_LINKED = """
definition test/nativeparentlinkedresource {
    relation editor: auth/user
    relation banned: auth/user
    relation team: scope/role
    relation admin: scope/role // rebac:const=admin
    relation sibling: test/nativeparentlinkedresource
    permission edit = editor + team->member + admin->member + sibling->edit
    permission veto = banned
}
definition test/nativeparentlinkedchild {
    relation resource: test/nativeparentlinkedresource // rebac:field=nativeparentlinkedresource_ptr
    relation owner: auth/user // rebac:field=owner
    relation team: scope/role
    permission edit = owner + resource->edit + team->member
    permission share = resource->edit - resource->veto
}
"""

MESSAGE_LINKS = """
definition scope/message {
    relation attached: test/nativeparentlinkedchild
    relation direct: auth/user
    permission edit = direct + attached->edit
    permission share = attached->share
}
"""


def test_the_parent_link_lends_what_the_same_record_lends_in_its_parent_table():
    schema = parse_zed(
        "definition auth/user {}\ndefinition scope/role { relation member: auth/user }\n"
        + PARENT_LINKED
        + MESSAGE_LINKS
    )
    narrowed = links.narrowed(schema)
    assert expression(narrowed, "scope/message", "edit") == "(direct + attached->lent(edit))"
    assert expression(narrowed, "test/nativeparentlinkedchild", "lent(edit)") == (
        "((owner + resource->lent(edit)) + team->member)"
    )
    assert expression(narrowed, "test/nativeparentlinkedresource", "lent(edit)") == (
        "((editor + team->member) + admin->member)"
    )
    # Under an exclusion in what is lent, the parent link is followed too.
    assert expression(narrowed, "test/nativeparentlinkedchild", "lent(share)") == (
        "(resource->lent(edit) - resource->veto)"
    )
    # In its own permission the parent link is a parent: followed whole.
    assert expression(narrowed, "test/nativeparentlinkedchild", "edit") == (
        "((owner + resource->edit) + team->member)"
    )


def test_an_arrow_to_several_types_has_one_name_on_each():
    schema = parse_zed(
        """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation sibling: scope/thread
    permission read = reader + sibling->read
}
definition scope/profile {
    relation direct: auth/user
    permission read = direct
}
definition scope/part {
    relation read: auth/user
}
definition scope/message {
    relation linked: scope/thread | scope/profile | scope/part
    permission read = linked->read
}
"""
    )
    narrowed = links.narrowed(schema)
    assert expression(narrowed, "scope/message", "read") == "linked->lent(read)"
    assert expression(narrowed, "scope/thread", "lent(read)") == "reader"
    # Where nothing differs, the generated name stands for the permission or the relation.
    assert expression(narrowed, "scope/profile", "lent(read)") == "read"
    assert expression(narrowed, "scope/part", "lent(read)") == "read"


def test_a_target_that_lends_everything_keeps_its_arrow():
    schema = parse_zed(SMALL.replace(" + sibling->read", ""))
    narrowed = links.narrowed(schema)
    assert expression(narrowed, "scope/part", "via_thread") == "thread->read"
    assert narrowed.get_permission("scope/thread", "lent(read)") is None


def test_generated_names_never_look_like_field_gates():
    schema = parse_zed(
        """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation sibling: scope/thread
    permission read__title = reader + sibling->read__title
    permission write__title = reader
}
definition scope/part {
    relation thread: scope/thread // rebac:field=message__thread
    permission read__title = thread->read__title
}
"""
    )
    narrowed = links.narrowed(schema)
    assert expression(narrowed, "scope/part", "read__title") == "thread->lent(read__title)"
    assert expression(narrowed, "scope/thread", "lent(read__title)") == "reader"
    for definition in narrowed.definitions:
        published = schema.get_definition(definition.resource_type)
        assert published is not None
        for verb in ("read", "write"):
            assert field_gated_actions(definition, verb) == field_gated_actions(published, verb)


# ``authenticated`` inside an expression is the actor's state, whatever a type declares under
# that name (``rebac.schema.ast.is_builtin_reference``); an arrow leads to the declared member.
BUILT_IN = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation keeper: scope/profile
    permission read = reader + keeper->read
    permission authenticated = reader + keeper->read
}
definition scope/profile {
    relation reader: auth/user
    permission read = reader
    permission authenticated = reader
}
definition scope/message {
    relation linked: scope/thread | scope/profile
    relation pub: auth/user:*
    relation direct: auth/user
    permission authenticated = linked->read
    permission read = pub - authenticated
    permission named = direct + linked->authenticated
}
"""


def test_a_built_in_name_is_the_actors_state_in_the_narrowed_policy():
    """Inside an expression the name is never versioned: ``pub - authenticated`` denies every
    authenticated actor in both policies. It was read as the declared permission, whose
    version under the exclusion takes away less (``excluded(authenticated)``), so the narrowed
    policy granted what the published one denies. Where an arrow leads to the name, the
    declared permission, a profile's unchanged one is emitted as itself: an alias would name
    the actor's state."""
    narrowed = links.narrowed(parse_zed(BUILT_IN))
    assert expression(narrowed, "scope/message", "read") == "(pub - authenticated)"
    assert narrowed.get_permission("scope/message", "excluded(authenticated)") is None
    assert (
        expression(narrowed, "scope/message", "named") == "(direct + linked->lent(authenticated))"
    )
    assert expression(narrowed, "scope/thread", "lent(authenticated)") == "reader"
    assert expression(narrowed, "scope/profile", "lent(authenticated)") == "reader"


def test_a_relation_named_as_a_built_in_is_refused():
    """A relation ``authenticated`` reads as the actor's state wherever an expression names it,
    and no generated permission could name it: the narrowing refuses the policy,
    deterministically (kept), as new input may not hold one (``rebac.W014``)."""
    schema = parse_zed(
        BUILT_IN.replace(
            "    permission read = reader\n    permission authenticated = reader\n}",
            "    relation authenticated: auth/user\n    permission read = reader\n}",
        )
    )
    with pytest.raises(
        links.NarrowingError, match=r"reserved built-in actors cannot be declared as relations"
    ) as refused:
        links.narrowed(schema)
    assert refused.value.kept


def test_the_narrowed_policy_cannot_be_published_by_mistake():
    """Its generated names are outside the grammar: rendered, it does not parse."""
    narrowed = links.narrowed(parse_zed(SMALL))
    with pytest.raises(ParseError):
        parse_zed(render_zed(narrowed))


def wide(width: int) -> Schema:
    """A permission that is a left-deep union of ``width`` link arrows."""
    base = parse_zed(
        """
definition auth/user {}
definition t/b {
    relation r: auth/user
    relation up: t/b
    permission read = r + up->read
}
definition t/a {
    relation l0: t/b
    permission read = l0->read
}
"""
    )
    union: Any = PermArrow("l0", "read")
    for _ in range(width):
        union = PermBinOp("+", union, PermArrow("l0", "read"))
    definitions = [
        d
        if d.resource_type != "t/a"
        else Definition("t/a", d.relations, (Permission("read", union),))
        for d in base.definitions
    ]
    return Schema(definitions=definitions)


def test_no_expression_the_compiler_accepts_is_too_deep_to_narrow(monkeypatch):
    def classify(definition: Any, relation: Any) -> links.Classification:
        kind = links.SAME_TYPE if relation.name == "up" else links.LINK
        return links.Classification(kind, "test")

    monkeypatch.setattr(links, "classify", classify)
    narrowed = links.narrowed(wide(3000))
    assert expression(narrowed, "t/b", "lent(read)") == "r"
    # A width the compiler accepts compiles narrowed too.
    CompileProgram.build(links.narrowed(wide(900)))


# ---------- What is kept and what is lost, on rows ----------

ROWS_POLICY = (
    SMALL.replace(
        "    relation banned: auth/user\n    permission read = thread->read",
        "    relation banned: auth/user\n"
        "    relation attached: test/nativeparentlinkedchild\n"
        "    permission edit = direct + attached->edit\n"
        "    permission share = attached->share\n"
        "    permission read = thread->read",
    )
    + PARENT_LINKED
    + """
definition blog/folder {
    relation cited: scope/message
    permission cites = cited->read
}
definition test/shelf {
    relation pinned: scope/thread
    permission read = pinned->read
}
"""
)


def build_rows() -> None:
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [
            ScopeMessage(id=1, thread_id=1),
            ScopeMessage(id=2, thread_id=1),
            ScopeMessage(id=3, thread_id=2),
        ]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 2, 3)]
    )
    ScopeProfile._base_manager.bulk_create(
        [ScopeProfile(id=1, owner_id=1), ScopeProfile(id=3, owner_id=3)]
    )


@pytest.fixture
def rows():
    local = install_schema(ROWS_POLICY)
    build_rows()
    return local


@pytest.fixture
def child(rows, django_user_model) -> str:
    """A multi-table child; its key is its row in the parent table."""
    with sudo(reason="test.fixture"):
        owner = django_user_model.objects.create(username="owner")
        return str(NativeParentLinkedChild.objects.create(name="child", owner=owner).pk)


def readable(model: Any, action: str = "read", actor: SubjectRef = MEMBER) -> set[int]:
    return set(model.objects.with_actor(actor).with_action(action).values_list("pk", flat=True))


def both_policies(settings, read: Any) -> tuple[Any, Any]:
    settings.REBAC_INHERIT_THROUGH_LINKS = True
    whole = read()
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    narrowed = read()
    return whole, narrowed


# Thread 1 is readable through its sibling, thread 2: what a thread does not lend.
THROUGH_SIBLING = (
    grant("scope/thread", 2, "reader"),
    grant("scope/thread", 1, "sibling", SubjectRef.of("scope/thread", "2")),
)


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_grant_reached_through_a_link_is_lost(rows, settings):
    rows.write_relationships(THROUGH_SIBLING)
    whole, narrowed = both_policies(
        settings, lambda: (readable(ScopeThread), readable(ScopePart, "via_thread"))
    )
    # The thread's own read keeps its hierarchy; the part reached it through two hops.
    assert whole == ({1, 2}, {1, 2, 3})
    assert narrowed == ({1, 2}, {3})


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_membership_and_a_constant_are_lent(rows, settings):
    rows.write_relationships(
        [
            grant("scope/thread", 1, "squad", SubjectRef.of("scope/role", "squad")),
            grant("scope/role", "squad", "member"),
            grant("scope/role", "admin", "member", ADMIN),
        ]
    )
    whole, narrowed = both_policies(
        settings,
        lambda: (
            readable(ScopePart, "via_thread"),
            readable(ScopeProfile, actor=ADMIN),
            readable(ScopePart, "via_thread", ADMIN),
        ),
    )
    assert whole == narrowed == ({1, 2}, {1, 3}, {1, 2, 3})


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_grant_reached_through_a_parent_is_kept(rows, settings):
    rows.write_relationships(THROUGH_SIBLING)
    whole, narrowed = both_policies(settings, lambda: (readable(ScopeMessage), readable(ScopePart)))
    assert whole == narrowed == ({1, 2, 3}, {1, 2, 3})


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_an_exclusion_reached_through_a_link_still_excludes(rows, settings):
    """``guarded = direct - profile->read``: profile 1 is read through its thread's sibling."""
    rows.write_relationships(
        [*THROUGH_SIBLING, *(grant("scope/message", n, "direct") for n in (1, 2))]
    )
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage, "guarded"))
    assert whole == narrowed == {2}
    # The same through a named permission under the exclusion.
    rows.write_relationships([grant("scope/message", n, "banned") for n in (1, 2)])
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage, "kept"))
    assert whole == narrowed == set()


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_an_exclusion_inside_what_is_lent_still_excludes(rows, settings):
    """``lens = profile->screened``: what a profile lends of ``screened`` keeps its exclusion."""
    rows.write_relationships(
        [grant("scope/profile", 1, "direct"), grant("scope/thread", 1, "blocked")]
    )
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage, "lens"))
    assert whole == narrowed == set()


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_under_two_subtractions_a_link_narrows_and_access_is_lost(rows, settings):
    rows.write_relationships(
        [*THROUGH_SIBLING, grant("scope/message", 1, "direct"), grant("scope/message", 1, "banned")]
    )
    whole, narrowed = both_policies(
        settings, lambda: (readable(ScopeMessage, "twice"), readable(ScopeMessage, "both"))
    )
    assert whole == ({1}, {1})
    assert narrowed == (set(), set())


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_same_type_relation_and_a_membership_are_followed_whole(rows, settings):
    rows.write_relationships(
        [
            grant("scope/thread", 1, "sibling", SubjectRef.of("scope/thread", "2")),
            grant("scope/thread", 2, "squad", SubjectRef.of("scope/role", "squad")),
            grant("scope/role", "squad", "member"),
        ]
    )
    whole, narrowed = both_policies(settings, lambda: readable(ScopeThread))
    assert whole == narrowed == {1, 2}


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_the_parent_link_is_followed_in_what_is_lent(rows, child, settings):
    attached = SubjectRef.of("test/nativeparentlinkedchild", child)
    rows.write_relationships(
        [
            grant("scope/message", 1, "attached", attached),
            grant("scope/message", 2, "attached", attached),
            grant("test/nativeparentlinkedresource", child, "editor"),
        ]
    )
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage, "edit"))
    assert whole == narrowed == {1, 2}
    # Excluded through the parent link inside what is lent: still excluded.
    rows.write_relationships([grant("test/nativeparentlinkedresource", child, "banned")])
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage, "share"))
    assert whole == narrowed == set()
    # Granted to the parent-table row through its own hierarchy: not lent.
    rows.delete_relationships(
        RelationshipFilter(
            resource_type="test/nativeparentlinkedresource", resource_id=child, relation="editor"
        )
    )
    with sudo(reason="test.fixture"):
        other = NativeParentLinkedResource.objects.create(name="other")
    rows.write_relationships(
        [
            grant(
                "test/nativeparentlinkedresource",
                child,
                "sibling",
                SubjectRef.of("test/nativeparentlinkedresource", str(other.pk)),
            ),
            grant("test/nativeparentlinkedresource", other.pk, "editor"),
        ]
    )
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage, "edit"))
    assert whole == {1, 2}
    assert narrowed == set()


# ---------- Generated names are not actions ----------


def allowed_by_decorator(action: str, resource_type: str, resource_id: str) -> bool:
    """Whether ``@require_permission(action, ...)`` lets the member call through."""

    @require_permission(
        action, resource_type=resource_type, resource_id=resource_id, actor_arg="actor"
    )
    def guarded(actor: SubjectRef) -> bool:
        return True

    try:
        return guarded(MEMBER)
    except PermissionDenied:
        return False


def answers(local: Any, model: Any, resource_type: str, action: str) -> dict[str, Any]:
    """What every public entry answers for ``action`` on every row of ``model``."""
    ids = [str(pk) for pk in model._base_manager.order_by("pk").values_list("pk", flat=True)]
    first = ObjectRef(resource_type, ids[0])
    found: dict[str, Any] = {
        "check_access": [
            local.check_access(subject=MEMBER, action=action, resource=ObjectRef(resource_type, i))
            for i in ids
        ],
        "has_access": local.has_access(subject=MEMBER, action=action, resource=first),
        "check_bulk": local.check_bulk_permissions(
            [CheckItem(MEMBER, "read", first), CheckItem(MEMBER, action, first)]
        )[1],
        "held": local.held(subject=MEMBER, action=action, resource_type=resource_type, ids=ids),
        "held_many": local.held_many(
            subject=MEMBER, actions=["read", action], resource_type=resource_type, ids=ids
        )[action],
        "accessible": sorted(
            local.accessible(subject=MEMBER, action=action, resource_type=resource_type)
        ),
        "grants_all": local.grants_all(subject=MEMBER, action=action, resource_type=resource_type),
        "lookup_subjects": list(
            local.lookup_subjects(resource=first, action=action, subject_type="auth/user")
        ),
        "with_action": readable(model, action),
        # A query that names its rows: what the row scope's "named" mode reads.
        "with_action named": set(
            model.objects.with_actor(MEMBER)
            .with_action(action)
            .filter(pk__in=[int(i) for i in ids])
            .values_list("pk", flat=True)
        ),
        "permission_expression": sorted(
            model._base_manager.annotate(
                allowed=permission_expression(model, action, actor=MEMBER)
            ).values_list("pk", "allowed")
        ),
        # The backend's own entries, called directly: a queryset's scope wraps its own call.
        "queryset_filter": sorted(
            model._base_manager.filter(
                local.queryset_filter(model=model, subject=MEMBER, action=action, using="default")
            ).values_list("pk", flat=True)
        ),
        "LocalBackend.permission_expression": sorted(
            model._base_manager.annotate(
                allowed=local.permission_expression(
                    model=model, subject=MEMBER, action=action, using="default", identity=F("pk")
                )
            ).values_list("pk", "allowed")
        ),
        "check_new": check_new(subject=MEMBER, action=action, resource_type=resource_type),
        "check_permission": check_permission(action, first, actor=MEMBER),
        "instance check_access": model._base_manager.get(pk=int(ids[0]))
        .with_actor(MEMBER)
        .check_access(action),
        "require_permission": allowed_by_decorator(action, resource_type, ids[0]),
        "row scope explained": rowscope.explain(model, MEMBER, action),
    }
    with evaluator_scope() as evaluator:
        found["evaluator"] = evaluator.check(local, subject=MEMBER, action=action, resource=first)
        found["scoped list"] = readable(model, action)
    if model is ScopeMessage:
        # Children read under parents a prefetch admitted: what structural prefetch reads.
        threads = ScopeThread.objects.with_actor(MEMBER).prefetch_related(
            Prefetch(
                "scopemessage_set",
                queryset=ScopeMessage.objects.with_actor(MEMBER).with_action(action),
            )
        )
        found["prefetched"] = sorted(
            (thread.pk, sorted(m.pk for m in thread.scopemessage_set.all())) for thread in threads
        )
    try:
        call_command("rebac", "explain", f"{resource_type}.{action}", stdout=StringIO())
    except CommandError as exc:
        found["explain"] = str(exc)
    return found


MODELS_OF = {
    "scope/thread": ScopeThread,
    "scope/profile": ScopeProfile,
    "scope/message": ScopeMessage,
    "scope/part": ScopePart,
}


READ_MECHANISMS = {
    "compiled": {},
    "row scope": {"REBAC_ROW_SCOPE": True},
    "named row scope": {"REBAC_ROW_SCOPE": "named"},
    "bounded walk": {"REBAC_BOUNDED_WALK": "decide"},
    "structural prefetch": {"REBAC_STRUCTURAL_PREFETCH": True},
}


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("read_mechanism", list(READ_MECHANISMS))
def test_no_public_entry_accepts_a_generated_name(rows, settings, read_mechanism):
    """Every public entry, with every generated name of the policy: the published answer,
    whichever read mechanism of proposal 0020 the read goes through."""
    for name, value in READ_MECHANISMS[read_mechanism].items():
        setattr(settings, name, value)
    rows.write_relationships(
        [
            *THROUGH_SIBLING,
            *(grant("scope/message", n, "direct") for n in (1, 2, 3)),
            grant("scope/profile", 1, "direct"),
        ]
    )
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    generated = [
        (definition.resource_type, permission.name)
        for definition in rows.evaluated_schema().definitions
        for permission in definition.permissions
        if links.is_generated(permission.name) and definition.resource_type in MODELS_OF
    ]
    assert {name for _type, name in generated} >= {"lent(read)", "excluded(read)", "excluded(hold)"}
    for resource_type, name in generated:
        model = MODELS_OF[resource_type]
        settings.REBAC_INHERIT_THROUGH_LINKS = True
        published = answers(rows, model, resource_type, name)
        settings.REBAC_INHERIT_THROUGH_LINKS = False
        assert answers(rows, model, resource_type, name) == published, (resource_type, name)
        assert published["check_access"][0].reason == f"unknown action: {resource_type}#{name}"


MESSAGE_IDS = ["1", "2", "3"]


def held_lists(local: Any, names: list[str]) -> dict[str, dict[str, set[str]]]:
    """Each entry that takes a list of names, asked for ``names`` on the messages: by name,
    the messages it answers allowed."""
    found: dict[str, dict[str, set[str]]] = {
        "held_many": local.held_many(
            subject=MEMBER, actions=names, resource_type="scope/message", ids=MESSAGE_IDS
        )
    }
    items = [
        CheckItem(MEMBER, name, ObjectRef("scope/message", pk))
        for name in names
        for pk in MESSAGE_IDS
    ]
    bulk: dict[str, set[str]] = {name: set() for name in names}
    for item, result in zip(items, local.check_bulk_permissions(items), strict=True):
        if result.allowed:
            bulk[item.action].add(item.resource.resource_id)
    found["check_bulk_permissions"] = bulk
    with evaluator_scope() as evaluator:
        # The evaluator's batch, through which the field gates decide a batch of rows.
        found["evaluator"] = evaluator._held_many(
            local,
            subject=MEMBER,
            model=ScopeMessage,
            resource_type="scope/message",
            actions=names,
            ids=MESSAGE_IDS,
            using="default",
        )
    return found


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("read_mechanism", ["compiled", "bounded walk"])
def test_a_declared_name_asked_beside_a_generated_one_is_answered_by_the_narrowed_policy(
    rows, settings, read_mechanism
):
    """A list that mixes declared and generated names: each declared name gets what it gets
    asked alone, the evaluated policy's answer, and each generated name the published
    policy's, an unknown action's. Answered wholly by the published policy, the declared
    names would get its wider answer; wholly by the narrowed one, the generated names would
    be actions."""
    for name, value in READ_MECHANISMS[read_mechanism].items():
        setattr(settings, name, value)
    # Message 1 is read under ``twice`` and ``both`` through its profile's thread's sibling
    # (published only), and by ``direct``, which every message lends (``lent(read)``).
    rows.write_relationships(
        [*THROUGH_SIBLING, grant("scope/message", 1, "direct"), grant("scope/message", 1, "banned")]
    )
    declared = ["twice", "both", "read"]
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    definition = rows.evaluated_schema().get_definition("scope/message")
    assert definition is not None
    generated = sorted(
        permission.name
        for permission in definition.permissions
        if links.is_generated(permission.name)
    )
    assert {"lent(read)", "excluded(hold)"} <= set(generated)
    narrowed = held_lists(rows, declared + generated)
    settings.REBAC_INHERIT_THROUGH_LINKS = True
    published = held_lists(rows, declared + generated)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    alone = held_lists(rows, declared)
    # The lists discriminate: the policies differ on a declared name, and the narrowed policy
    # grants something under a generated one.
    assert alone["held_many"]["twice"] != published["held_many"]["twice"]
    assert rows._check_access(
        subject=MEMBER, action="lent(read)", resource=ObjectRef("scope/message", "1")
    ).allowed
    for entry, answer in narrowed.items():
        for name in declared:
            assert answer[name] == alone[entry][name], (entry, name)
        for name in generated:
            assert answer[name] == published[entry][name] == set(), (entry, name)
    # In the other order, and with the generated names alone.
    for entry, answer in held_lists(rows, generated + declared).items():
        assert answer == narrowed[entry], entry
    assert all(
        names == {name: set() for name in generated}
        for names in held_lists(rows, generated).values()
    )


# SMALL with a permission that excludes ``guarded``: ``excluded(guarded)`` is generated.
EXCLUDING = ROWS_POLICY.replace(
    "    permission guarded = direct - profile->read\n",
    "    permission guarded = direct - profile->read\n    permission outer = direct - guarded\n",
)


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_an_excluded_version_is_not_an_action(settings):
    """The reviewer's case: ``excluded(guarded)`` grants rows ``guarded`` withholds, inside."""
    local = install_schema(EXCLUDING)
    build_rows()
    local.write_relationships([*THROUGH_SIBLING, grant("scope/message", 1, "direct")])
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    assert local.evaluated_schema().get_permission("scope/message", "excluded(guarded)") is not None
    assert readable(ScopeMessage, "guarded") == set()
    assert readable(ScopeMessage, "excluded(guarded)") == set()
    assert not local.check_access(
        subject=MEMBER, action="excluded(guarded)", resource=ObjectRef("scope/message", "1")
    ).allowed


DECLARED_LOOKALIKE = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation sibling: scope/thread
    permission read = reader + sibling->read
}
definition scope/profile {
    relation direct: auth/user
    LOOKALIKE
}
definition scope/message {
    relation linked: scope/thread | scope/profile
    relation direct: auth/user
    relation banned: auth/user
    permission read = linked->read
    permission twice = direct - (banned - linked->read)
}
"""


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize(
    "lookalike", ["permission lent__read = direct", "relation lent__read: auth/user"]
)
def test_a_declared_name_on_a_type_an_arrow_reaches_is_never_evaluated_for_it(lookalike, settings):
    """The reviewer's case: a type the arrow reaches declares no ``read`` but a name that
    looked generated. Generated names cannot be declared, so it is just a name."""
    local = install_schema(DECLARED_LOOKALIKE.replace("LOOKALIKE", lookalike))
    build_rows()
    local.write_relationships(
        [
            grant("scope/message", 1, "linked", SubjectRef.of("scope/profile", "1")),
            grant("scope/message", 1, "direct"),
            grant("scope/message", 1, "banned"),
            grant("scope/profile", 1, "direct"),
            grant("scope/profile", 1, "lent__read"),
        ]
        if lookalike.startswith("relation")
        else [
            grant("scope/message", 1, "linked", SubjectRef.of("scope/profile", "1")),
            grant("scope/message", 1, "direct"),
            grant("scope/message", 1, "banned"),
            grant("scope/profile", 1, "direct"),
        ]
    )
    for action in ("read", "twice"):
        whole, narrowed = both_policies(
            settings, lambda action=action: readable(ScopeMessage, action)
        )
        assert narrowed <= whole, action
    assert readable(ScopeMessage) == set()


CREATE = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation sibling: scope/thread
    relation blocked: auth/user
    permission read = reader + sibling->read
    permission veto = blocked + sibling->veto
}
definition scope/message {
    relation linked: scope/thread
    relation direct: auth/user
    permission create = linked->read
    permission guarded = direct - linked->veto
}
"""


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_check_new_follows_the_narrowed_policy():
    """A proposed row's arrows hop into generated permissions, which the walk may name."""
    from django.test import override_settings

    local = install_schema(CREATE)
    build_rows()
    local.write_relationships(
        [
            grant("scope/thread", 2, "reader"),
            grant("scope/thread", 1, "sibling", SubjectRef.of("scope/thread", "2")),
            grant("scope/thread", 2, "blocked"),
            grant("scope/thread", 3, "sibling", SubjectRef.of("scope/thread", "2")),
        ]
    )
    found = {}
    for value in (True, False):
        with override_settings(REBAC_INHERIT_THROUGH_LINKS=value):
            found[value] = tuple(
                check_new(
                    subject=MEMBER,
                    action="create",
                    resource_type="scope/message",
                    relationships={"linked": [SubjectRef.of("scope/thread", str(pk))]},
                ).allowed
                for pk in (1, 2)
            )
    # Through thread 1's sibling: lost. Thread 2's own reader: kept.
    assert found[True] == (True, True)
    assert found[False] == (False, True)


# ---------- Every surface sees one policy, keyed by the setting ----------


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("read_mechanism", ["none", "walk", "rows"])
def test_every_read_path_sees_the_narrowed_policy(rows, settings, read_mechanism):
    """Also through the bounded walk (``held``) and the row scope (querysets), proposal 0020."""
    rows.write_relationships(THROUGH_SIBLING)
    settings.REBAC_BOUNDED_WALK = "decide" if read_mechanism == "walk" else False
    settings.REBAC_ROW_SCOPE = read_mechanism == "rows"
    for value, expected in ((True, {"1", "2", "3"}), (False, {"3"})):
        settings.REBAC_INHERIT_THROUGH_LINKS = value
        found = decisions(rows, MEMBER, ScopePart, "scope/part", "via_thread")
        found["lookup_subjects"] = {
            pk
            for pk in ("1", "2", "3")
            if MEMBER
            in rows.lookup_subjects(
                resource=ObjectRef("scope/part", pk), action="via_thread", subject_type="auth/user"
            )
        }
        with evaluator_scope():
            found["profiled"] = listed(MEMBER, ScopePart, "scope/part", "via_thread")
        assert all(ids == expected for ids in found.values()), (value, found)


@pytest.mark.django_db
def test_with_the_setting_true_nothing_is_narrowed(rows, monkeypatch):
    def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("narrowed with the setting True")

    monkeypatch.setattr(links, "narrowed", refused)
    monkeypatch.setattr(links, "narrowed_tagged", refused)
    assert rows.evaluated_schema() is rows._published_schema() is rows.schema()
    assert readable(ScopePart, "via_thread") == set()
    with evaluator_scope():
        assert readable(ScopePart, "via_thread") == set()


def statement(model: Any, action: str) -> str:
    with CaptureQueriesContext(connection) as captured:
        list(model.objects.with_actor(MEMBER).with_action(action).values_list("pk", flat=True))
    return captured.captured_queries[-1]["sql"]


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_with_the_setting_true_statements_are_the_published_policys(rows, monkeypatch):
    """True compiles the published policy: byte-identical before and after a False round trip."""
    published = render_zed(rows._published_schema())
    first = statement(ScopePart, "via_thread")
    flip(monkeypatch, False)
    narrowed = statement(ScopePart, "via_thread")
    flip(monkeypatch, True)
    again = statement(ScopePart, "via_thread")
    assert narrowed != first
    assert again == first
    assert render_zed(rows._published_schema()) == published
    assert "lent(" not in published


@contextmanager
def scoped(value: bool) -> Iterator[None]:
    with evaluator_scope() if value else nullcontext():
        yield


# Outside a transaction, so that an evaluator scope keeps decisions and profiles.
@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_a_change_of_the_setting_takes_effect_with_no_signal_and_no_reset(rows, monkeypatch):
    """Kept statements, kept policies, kept decisions and profiles are keyed by the setting."""
    rows.write_relationships(
        [*THROUGH_SIBLING, grant("test/shelf", 1, "pinned", SubjectRef.of("scope/thread", "1"))]
    )
    part = ObjectRef("scope/part", "1")
    # A shelf reads no model column, so the evaluator keeps decisions about it.
    folder = ObjectRef("test/shelf", "1")
    assert rows._cache_generation("test/shelf") is not None
    for scope in (False, True):
        with scoped(scope):
            for value in (True, False, True):
                flip(monkeypatch, value)
                # Thread 1 is read through its sibling: lent by neither part 1 nor the shelf.
                assert readable(ScopePart, "via_thread") == ({1, 2, 3} if value else {3})
                answer = rows.check_access(subject=MEMBER, action="via_thread", resource=part)
                assert answer.allowed is value, (scope, value)
                assert rows.held(
                    subject=MEMBER, action="via_thread", resource_type="scope/part", ids=["1", "3"]
                ) == ({"1", "3"} if value else {"3"})
                assert rows.has_access(subject=MEMBER, action="read", resource=folder) is value
                evaluator = current_evaluator()
                if evaluator is not None:
                    # The scope's decision cache, as model instances ask it.
                    cached = evaluator.check(rows, subject=MEMBER, action="read", resource=folder)
                    assert cached.allowed is value, (scope, value)


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_the_schema_facts_of_one_policy_are_not_the_others(rows, monkeypatch):
    """A folder that cites a message reads a model column through the message's thread in the
    published policy, and none in the narrowed one: its decisions are cached in the narrowed
    policy only. The published policy must not reuse the narrowed one's facts."""
    rows.write_relationships(
        [
            grant("blog/folder", 1, "cited", SubjectRef.of("scope/message", "1")),
            grant("scope/thread", 1, "reader"),
        ]
    )
    folder = ObjectRef("blog/folder", "1")
    with evaluator_scope() as evaluator:
        flip(monkeypatch, False)
        assert not evaluator.check(rows, subject=MEMBER, action="cites", resource=folder).allowed
        flip(monkeypatch, True)
        assert evaluator.check(rows, subject=MEMBER, action="cites", resource=folder).allowed
        with sudo(reason="test.move"):
            ScopeMessage._base_manager.filter(pk=1).update(thread_id=2)
        assert not evaluator.check(rows, subject=MEMBER, action="cites", resource=folder).allowed


@pytest.mark.django_db
def test_nothing_is_narrowed_when_nothing_is_enforced(rows, settings):
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    settings.REBAC_ENABLED = False
    assert readable(ScopePart, "via_thread") == {1, 2, 3}


# ---------- A narrowing that cannot be built ----------


@pytest.mark.django_db
def test_a_narrowing_that_fails_fails_reads_closed_and_leaves_writes_alone(
    rows, settings, monkeypatch
):
    from rebac.checks import check_inherit_through_links

    calls = {"classify": 0}

    def broken(definition: Any, relation: Any) -> Any:
        calls["classify"] += 1
        raise RuntimeError("cannot classify")

    monkeypatch.setattr(links, "classify", broken)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    for attempt in range(1, 4):
        with pytest.raises(links.NarrowingError, match=r"rebac\.E025"):
            readable(ScopeMessage)
        # An unexpected failure is not kept: each operation narrows again.
        assert calls["classify"] >= attempt
    # Writes read the published policy.
    rows.write_relationships([grant("scope/message", 1, "direct")])
    with sudo(reason="test.write"):
        ScopeMessage.objects.filter(pk=1).update(thread_id=2)
    issues = {issue.id: issue for issue in check_inherit_through_links()}
    assert set(issues) == {"rebac.W012", "rebac.E025"}
    with pytest.raises(CommandError, match=r"rebac\.E025"):
        call_command("rebac", "explain", "scope/message.read", stdout=StringIO())


# ---------- The cost of narrowing ----------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("overridden", "timed", "expected"),
    [(True, True, 2), (True, False, 2), (False, False, 1)],
    ids=["deadline ahead", "no deadline", "no override"],
)
def test_one_narrowing_per_composed_policy(settings, monkeypatch, overridden, timed, expected):
    """While an override deadline is ahead the policy is prepared at each use; its
    narrowings are not. One per composition, kept: the snapshot's own (which check_new and
    the compiled policy's effective composition share), and, where there are override rows,
    the tagged one whose deadlines the statements witness."""
    permissions = stored_policy(settings, "denormalized", message="direct + linked->read")
    build_rows()
    backend().write_relationships(
        [
            grant("scope/thread", 1, "reader"),
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
        ]
    )
    if overridden:
        deadline = {"expires_at": timezone.now() + timedelta(minutes=5)} if timed else {}
        override(permissions[("scope/thread", "read")], "extend", "squad->member", **deadline)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    calls = {"narrowings": 0}
    original = links.narrowed_tagged

    def counted(tagged: Any) -> Any:
        calls["narrowings"] += 1
        return original(tagged)

    monkeypatch.setattr(links, "narrowed_tagged", counted)
    monkeypatch.setattr(
        links, "narrowed", lambda *a, **k: pytest.fail("narrowed outside the cache")
    )
    for _ in range(5):
        assert readable(ScopeMessage) == {1}
        backend().check_access(
            subject=MEMBER, action="read", resource=ObjectRef("scope/message", "1")
        )
        check_new(
            subject=MEMBER,
            action="read",
            resource_type="scope/message",
            relationships={"linked": [SubjectRef.of("scope/thread", "1")]},
        )
    assert calls["narrowings"] == expected


@pytest.mark.django_db
def test_a_kept_narrowing_serves_only_the_object_it_was_made_from(rows, settings):
    """Narrowings are kept by the identity of what they were made from, and Python reuses an
    identity once its object is gone: a kept entry whose source is another object at that
    identity is made again, never served."""
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    snapshot = rows._load_schema_snapshot()
    published = snapshot.schema
    other = parse_zed(SMALL)
    stale = links.narrowed(other)
    rows._narrowed[id(published)] = (other, stale)
    assert rows._narrowed_schema(snapshot) is not stale
    assert rows.evaluated_schema() is rows._narrowed_schema(snapshot)
    tagged = rows._narrowed_composition(other, ())
    rows._compositions[(id(published), ())] = (other, tagged)
    assert rows._narrowed_composition(published, ()) is not tagged


# ---------- Overrides ----------

STORED = {
    "auth/user": ({}, {}),
    "scope/role": ({"member": [{"type": "auth/user"}]}, {}),
    "scope/thread": (
        {
            "reader": [{"type": "auth/user"}],
            "squad": [{"type": "scope/role"}],
            "sibling": [{"type": "scope/thread"}],
        },
        {"read": "reader + sibling->read"},
    ),
    "scope/message": (
        {"linked": [{"type": "scope/thread"}], "direct": [{"type": "auth/user"}]},
        {"read": "direct"},
    ),
}


def stored_policy(
    settings, storage: str, message: str = "direct", thread: str | None = None
) -> dict[tuple[str, str], Any]:
    """The policy stored through its write owners, so that overrides compose with it.

    ``message`` and ``thread`` replace the stored expressions of their reads.
    """
    from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
    from rebac.models.schema_write import schema_index_write

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    reset_backend()
    found = {}
    replaced = {"scope/message": message, "scope/thread": thread}
    with schema_index_write("default"):
        for type_, (relations, permissions) in STORED.items():
            definition = SchemaDefinition.objects.create(resource_type=type_)
            for name, subjects in relations.items():
                SchemaRelation.objects.create(
                    definition=definition, name=name, allowed_subjects=subjects
                )
            for name, text in permissions.items():
                found[(type_, name)] = SchemaPermission.objects.create(
                    definition=definition, name=name, expression=replaced.get(type_) or text
                )
    return found


def override(permission: Any, kind: str, text: str, **fields: Any) -> Any:
    from rebac.models import SchemaOverride, SchemaPermission

    return SchemaOverride.objects.create(
        kind=kind,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=permission.pk,
        expression=text,
        reason="test",
        **fields,
    )


def tags_are_held(tagged: Any) -> bool:
    held = {
        id(node)
        for definition in tagged.schema.definitions
        for permission in definition.permissions
        for node in links._nodes(permission.expression)
    }
    return set(tagged.arms) <= held and set(tagged.sites) <= held


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_a_timed_override_arm_is_narrowed_and_keeps_its_deadline(settings, storage):
    permissions = stored_policy(settings, storage)
    build_rows()
    backend().write_relationships(
        [
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
            grant("scope/message", 2, "linked", SubjectRef.of("scope/thread", "2")),
            grant("scope/thread", 1, "reader"),
            grant("scope/thread", 2, "sibling", SubjectRef.of("scope/thread", "1")),
        ]
    )
    deadline = timezone.now() + timedelta(minutes=5)
    override(permissions[("scope/message", "read")], "extend", "linked->read", expires_at=deadline)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    from rebac.compile import read

    tagged = read._policy(backend()).tagged
    assert tags_are_held(tagged) and len(tagged.arms) == 1
    for instant, expected in ((deadline - timedelta(seconds=1), {1}), (deadline, set())):
        with patch("django.utils.timezone.now", return_value=instant):
            read.reset()
            # The arm lends the thread's reader, not its sibling; after its deadline nothing.
            assert readable(ScopeMessage) == expected
            for pk in (1, 2, 3):
                answer = backend().check_access(
                    subject=MEMBER, action="read", resource=ObjectRef("scope/message", str(pk))
                )
                assert answer.allowed is (pk in expected), (instant, pk)


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_timed_union_arm_keeps_its_deadline_in_what_is_lent(settings):
    """``reader + sibling->read`` loses its sibling in what a thread lends: the union that
    remains is still the timed arm, and grants nothing after its deadline."""
    permissions = stored_policy(
        settings, "denormalized", message="direct + linked->read", thread="squad->member"
    )
    build_rows()
    backend().write_relationships(
        [
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
            grant("scope/thread", 1, "reader"),
        ]
    )
    deadline = timezone.now() + timedelta(minutes=5)
    override(
        permissions[("scope/thread", "read")],
        "extend",
        "reader + sibling->read",
        expires_at=deadline,
    )
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    from rebac.compile import read

    assert tags_are_held(read._policy(backend()).tagged)
    for instant, expected in ((deadline - timedelta(seconds=1), {1}), (deadline, set())):
        with patch("django.utils.timezone.now", return_value=instant):
            read.reset()
            whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage))
            assert whole == narrowed == expected, instant


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_an_override_of_the_linked_permission_reaches_what_it_lends(settings):
    """Narrowed after composition: a disable on the thread's read applies to what it lends."""
    permissions = stored_policy(settings, "denormalized", message="direct + linked->read")
    build_rows()
    backend().write_relationships(
        [
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
            grant("scope/thread", 1, "reader"),
        ]
    )
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage))
    assert whole == narrowed == {1}
    override(permissions[("scope/thread", "read")], "disable", "reader")
    whole, narrowed = both_policies(settings, lambda: readable(ScopeMessage))
    assert whole == narrowed == set()


# An extend whose arm is an exclusion, on a permission read under an exclusion in what a
# record lends. Narrowed, a message reads its child's ``lent(share)``, which excludes the
# parent table's ``excluded(lent(veto))``: in what a record lends the parent link is followed
# under an exclusion too. There the arm's subtrahend, an arrow within the type, is removed, and
# what remains of the arm holds until its deadline, as the arm does.
ARM_UNDER_AN_EXCLUSION = """
definition auth/user {}
definition test/nativeparentlinkedresource {
    relation editor: auth/user
    relation banned: auth/user
    relation sibling: test/nativeparentlinkedresource
    permission edit = editor + sibling->edit
    permission veto = banned
}
definition test/nativeparentlinkedchild {
    relation resource: test/nativeparentlinkedresource // rebac:field=nativeparentlinkedresource_ptr
    permission share = resource->edit - resource->veto
}
definition scope/message {
    relation attached: test/nativeparentlinkedchild
    permission read = attached->share
}
"""
# A tighten whose operand is an arrow through a link, on a permission a record lends.
# Narrowed, a message reads its part's ``lent(read)``, where the operand is removed: the
# intersection that remains is the tighten's site, which after its deadline is its left
# operand.
SITE_IN_WHAT_IS_LENT = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/part {
    relation direct: auth/user
    relation linked: scope/thread
    permission read = direct
}
definition scope/message {
    relation linked: scope/part
    permission read = linked->read
}
"""


def child_rows(django_user_model: Any) -> list[RelationshipTuple]:
    """A multi-table child attached to message 1, whose parent table row the member edits."""
    with sudo(reason="test.fixture"):
        owner = django_user_model.objects.create(username="owner")
        child = str(NativeParentLinkedChild.objects.create(name="child", owner=owner).pk)
    return [
        grant("scope/message", 1, "attached", SubjectRef.of("test/nativeparentlinkedchild", child)),
        grant("test/nativeparentlinkedresource", child, "editor"),
    ]


def part_rows(django_user_model: Any) -> list[RelationshipTuple]:
    """Part 1, linked to message 1, which the member reads directly and through thread 1."""
    return [
        grant("scope/message", 1, "linked", SubjectRef.of("scope/part", "1")),
        grant("scope/part", 1, "direct"),
        grant("scope/part", 1, "linked", SubjectRef.of("scope/thread", "1")),
        grant("scope/thread", 1, "reader"),
    ]


# The policy, the permission overridden, the override, the rows, and the messages the member
# reads (published, narrowed) before the deadline and after it.
TIMED_OVERRIDES: dict[str, tuple[str, tuple[str, str], str, str, Any, dict[str, Any]]] = {
    "an arm that is an exclusion": (
        ARM_UNDER_AN_EXCLUSION,
        ("test/nativeparentlinkedresource", "veto"),
        "extend",
        "editor - sibling->edit",
        child_rows,
        {"before": (set(), set()), "after": ({1}, {1})},
    ),
    "a site that is an intersection": (
        SITE_IN_WHAT_IS_LENT,
        ("scope/part", "read"),
        "tighten",
        "linked->read",
        part_rows,
        {"before": ({1}, set()), "after": ({1}, {1})},
    ),
}


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("state", ["pending", "expired"])
@pytest.mark.parametrize("case", list(TIMED_OVERRIDES))
def test_a_tagged_node_whose_operand_is_removed_keeps_its_deadline(
    settings, django_user_model, storage, state, case
):
    """The narrowing simplifies an override's tagged node only where what replaces it means
    the same before its deadline and after it. Here an operand of the node is removed in
    what a record lends: the node keeps its tag, so the narrowed policy grants, before the
    deadline and after it, exactly what it means then. Dropped, the narrowed policy would
    deny after the deadline what the published policy, and its own meaning, grant there.
    ``pending``: read before the deadline and at it; ``expired``: stored after it."""
    from rebac.compile import read

    policy, target, kind, text, relationships, expected = TIMED_OVERRIDES[case]
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    permissions = store(policy)
    build_rows()
    backend().write_relationships(relationships(django_user_model))
    now = timezone.now()
    deadline = now + timedelta(minutes=5) if state == "pending" else now - timedelta(minutes=5)
    override(permissions[target], kind, text, expires_at=deadline)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    tagged = read._policy(backend()).tagged
    assert tags_are_held(tagged) and len(tagged.arms) + len(tagged.sites) == 2
    instants = (
        [("before", deadline - timedelta(seconds=1)), ("after", deadline)]
        if state == "pending"
        else [("after", now)]
    )
    for when, instant in instants:
        with patch("django.utils.timezone.now", return_value=instant):
            read.reset()
            assert both_policies(settings, lambda: readable(ScopeMessage)) == expected[when], when
            checked = {
                pk
                for pk in (1, 2, 3)
                if backend()
                .check_access(
                    subject=MEMBER, action="read", resource=ObjectRef("scope/message", str(pk))
                )
                .allowed
            }
            assert checked == expected[when][1], when


def test_a_node_with_no_tag_is_simplified_where_an_operand_is_removed():
    """What a part lends of an intersection with an arrow through a link is nothing, and of
    a union with one, its other operand: no node is kept that means nothing."""
    narrowed = links.narrowed(
        parse_zed(
            SITE_IN_WHAT_IS_LENT.replace(
                "    permission read = direct\n",
                "    permission read = direct & linked->read\n"
                "    permission either = direct + linked->read\n",
            ).replace(
                "    permission read = linked->read\n}",
                "    permission read = linked->read\n    permission either = linked->either\n}",
            )
        )
    )
    assert expression(narrowed, "scope/message", "read") == "linked->lent(read)"
    assert expression(narrowed, "scope/part", "lent(read)") == "nil"
    assert expression(narrowed, "scope/part", "lent(either)") == "direct"


# ---------- The invariant on random policies ----------


def shapes(rng: random.Random) -> Any:
    """``shaped(grants, denials)``: a union, an exclusion, an exclusion kept beside a union, an
    intersection, an exclusion of an exclusion, or a grant of the type's own relation less a
    union (only the exclusion reaches through links). ``simple=True`` keeps to a union, one
    exclusion or a guarded one: a ``hold`` that other types' arrows reach, so that statements
    stay small enough to compile many."""

    def union(atoms: list[str]) -> str:
        return " + ".join(rng.sample(atoms, rng.randint(1, min(2, len(atoms)))))

    def shaped(grants: list[str], denials: list[str], simple: bool = False) -> str:
        choices = (
            ["union", "minus", "guarded"]
            if simple
            else ["union", "minus", "kept", "both", "nested", "nested", "guarded"]
        )
        shape = rng.choice(choices)
        if shape == "guarded":
            return f"{grants[0]} - ({union(denials)})"
        if shape == "union":
            return union(grants)
        if shape == "minus":
            return f"({union(grants)}) - ({union(denials)})"
        if shape == "kept":
            return f"(({union(grants)}) - ({union(denials)})) + ({union(grants)})"
        if shape == "both":
            return f"({union(grants)}) & ({union(grants)})"
        return f"({union(grants)}) - (({union(denials)}) - ({union(grants)}))"

    return shaped


def linked(seed: int) -> tuple[str, list[RelationshipTuple], dict[int, str]]:
    """``(schema, tuples, thread labels)`` over threads, profiles, messages and parts.

    A part reaches its message by foreign key and its thread through two hops and a
    filter over another table; a message reaches its profile through a reverse
    one-to-one and a stored relation to two types, where ``seen`` is a relation of
    one and a permission of the other; a profile reaches its thread through two hops
    and a stored relation; a thread reaches its sibling, its role (a constant and a
    membership) and everyone (a wildcard). A named permission (``hold``) is used on
    either side of an exclusion.
    """
    rng = random.Random(seed)
    shaped = shapes(rng)
    # The first atom of each is the type's own relation (``guarded``).
    thread = ["reader", "squad->member", "admin->member"]
    profile = ["direct", "thread->read", "pinned->read", "thread->hold"]
    message = [
        "direct",
        "thread->read",
        "profile->read",
        "cited->hold",
        "cited->seen",
        "admin->member",
    ]
    part = ["direct", "message->read", "thread->read", "lead->read", "first->hold"]
    schema = f"""
definition auth/user {{}}
definition auth/group {{ relation member: auth/user }}
definition scope/role {{ relation member: auth/user | auth/group#member }}
definition scope/thread {{
    relation admin: scope/role // rebac:const=admin
    relation squad: scope/role
    relation sibling: scope/thread
    relation reader: auth/user | auth/group#member | auth/user:*
    relation blocked: auth/user
    relation seen: auth/user
    permission hold = {shaped(thread, ["blocked", "squad->member"], simple=True)}
    permission read = {shaped([*thread, "sibling->hold", "hold"], ["blocked", "hold"])}
}}
definition scope/profile {{
    relation thread: scope/thread // rebac:field=owner__thread
    relation pinned: scope/thread
    relation direct: auth/user
    relation banned: auth/user
    permission seen = {shaped(["direct", "thread->read", "pinned->hold"], ["banned"])}
    permission hold = {shaped(profile, ["banned", "thread->hold", "pinned->read"], simple=True)}
    permission read = {shaped([*profile, "hold"], ["banned", "pinned->hold", "hold"])}
}}
definition scope/message {{
    relation admin: scope/role // rebac:const=admin
    relation thread: scope/thread // rebac:field=thread
    relation profile: scope/profile // rebac:field=profile
    relation cited: scope/profile | scope/thread
    relation direct: auth/user | auth/group#member
    relation banned: auth/user
    permission hold = {shaped(message, ["banned", "profile->read", "cited->read"], simple=True)}
    permission read = {shaped([*message, "hold"], ["banned", "profile->hold", "hold", "cited->seen"])}
}}
definition scope/part {{
    relation message: scope/message // rebac:field=message
    relation thread: scope/thread // rebac:field=message__thread
    relation lead: scope/message // rebac:field={{"path":"message","filters":{{"message__thread__label":"hidden"}}}}
    relation first: scope/message // rebac:field={{"path":"message","filters":{{"fragment_id":1}}}}
    relation direct: auth/user
    relation banned: auth/user
    permission hold = {shaped(part, ["banned", "lead->hold"], simple=True)}
    permission read = {shaped([*part, "hold"], ["banned", "lead->hold", "thread->read", "hold"])}
}}
"""
    squad = SubjectRef.of("scope/role", "squad")
    group = SubjectRef.of("auth/group", "g", "member")
    candidates = [
        grant("auth/group", "g", "member"),
        grant("auth/group", "g", "member", ADMIN),
        grant("scope/role", "admin", "member", ADMIN),
        grant("scope/role", "squad", "member"),
        grant("scope/role", "squad", "member", group),
        *(grant("scope/thread", n, "squad", squad) for n in (1, 2)),
        *(grant("scope/thread", n, "reader", actor) for n in (1, 2) for actor in (MEMBER, ADMIN)),
        grant("scope/thread", 2, "reader", group),
        grant("scope/thread", 1, "reader", EVERYONE),
        *(grant("scope/thread", n, "blocked", actor) for n in (1, 2) for actor in (MEMBER, ADMIN)),
        *(grant("scope/thread", n, "seen", actor) for n in (1, 2) for actor in (MEMBER, ADMIN)),
        grant("scope/profile", 1, "pinned", SubjectRef.of("scope/thread", "2")),
        grant("scope/profile", 3, "pinned", SubjectRef.of("scope/thread", "1")),
        *(grant("scope/profile", n, r, a) for n in (1, 3) for r in ("direct", "banned")
          for a in (MEMBER, ADMIN)),
        grant("scope/message", 2, "cited", SubjectRef.of("scope/profile", "1")),
        grant("scope/message", 3, "cited", SubjectRef.of("scope/thread", "1")),
        grant("scope/message", 1, "cited", SubjectRef.of("scope/profile", "3")),
        *(grant("scope/message", n, r, a) for n in (1, 2, 3) for r in ("direct", "banned")
          for a in (MEMBER, ADMIN)),
        grant("scope/message", 2, "direct", group),
        *(grant("scope/part", n, r, a) for n in (1, 2, 3) for r in ("direct", "banned")
          for a in (MEMBER, ADMIN)),
    ]  # fmt: skip
    # Always: thread 1 is read through its sibling, and the member is in the squad.
    always = [
        grant("scope/thread", 1, "sibling", SubjectRef.of("scope/thread", "2")),
        grant("scope/thread", 2, "reader"),
        grant("scope/role", "squad", "member"),
    ]
    tuples = always + [item for item in candidates if item not in always and rng.random() < 0.45]
    labels = {n: rng.choice(["thread", "hidden"]) for n in (1, 2)}
    return schema, tuples, labels


def store(schema_text: str) -> dict[tuple[str, str], Any]:
    """Store a policy through its write owners: the backend then reads it, overrides included."""
    from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
    from rebac.models.schema_write import schema_index_write

    found = {}
    with schema_index_write("default"):
        for definition in parse_zed(schema_text).definitions:
            row = SchemaDefinition.objects.create(resource_type=definition.resource_type)
            for relation in definition.relations:
                SchemaRelation.objects.create(
                    definition=row,
                    name=relation.name,
                    allowed_subjects=[
                        {
                            "type": subject.type,
                            "relation": subject.relation,
                            "wildcard": subject.wildcard,
                        }
                        for subject in relation.allowed_subjects
                    ],
                    backing=backing_to_dict(relation.backing),
                )
            for permission in definition.permissions:
                found[(definition.resource_type, permission.name)] = (
                    SchemaPermission.objects.create(
                        definition=row,
                        name=permission.name,
                        expression=_render_expr(permission.expression),
                    )
                )
    reset_backend()
    return found


def random_overrides(
    rng: random.Random, permissions: dict[tuple[str, str], Any], atoms: dict[str, list[str]]
) -> None:
    """Up to two overrides of reads: extend, disable or tighten, untimed, ahead or passed."""
    now = timezone.now()
    for _ in range(rng.randint(1, 2)):
        type_ = rng.choice(sorted(atoms))
        kind = rng.choice(["extend", "disable", "tighten"])
        text = " + ".join(rng.sample(atoms[type_], rng.randint(1, 2)))
        when = rng.choice([None, now + timedelta(minutes=5), now - timedelta(minutes=5)])
        override(permissions[(type_, "read")], kind, text, **({"expires_at": when} if when else {}))


def listed(actor: SubjectRef, model: Any, resource_type: str, action: str) -> set[str]:
    rows = model.objects.with_actor(actor).with_action(action)
    return {str(pk) for pk in rows.values_list("pk", flat=True)}


def decisions(
    local: Any, actor: SubjectRef, model: Any, resource_type: str, action: str
) -> dict[str, set[str]]:
    """The ids ``actor`` holds ``action`` on, as each read path answers."""
    ids = [str(pk) for pk in model._base_manager.order_by("pk").values_list("pk", flat=True)]
    projected = model._base_manager.annotate(
        allowed=permission_expression(model, action, actor=actor)
    )
    others = [name for name in ("hold", "read") if name != action]
    return {
        "list": listed(actor, model, resource_type, action),
        "expression": {
            str(pk) for pk, allowed in projected.values_list("pk", "allowed") if allowed
        },
        "held": set(local.held(subject=actor, action=action, resource_type=resource_type, ids=ids)),
        "held_many": set(
            local.held_many(
                subject=actor, actions=[action, *others], resource_type=resource_type, ids=ids
            )[action]
        ),
        "check": {
            value
            for value in ids
            if local.check_access(
                subject=actor, action=action, resource=ObjectRef(resource_type, value)
            ).allowed
        },
        "accessible": set(
            local.accessible(subject=actor, action=action, resource_type=resource_type)
        ),
    }


def lesser_paths(
    local: Any, actor: SubjectRef, model: Any, resource_type: str, action: str
) -> dict[str, set[Any]]:
    """Paths that answer other questions than the row sets: each must only shrink."""
    ids = [str(pk) for pk in model._base_manager.order_by("pk").values_list("pk", flat=True)]
    wildcard = SubjectRef.of(actor.subject_type, "*")
    found: dict[str, set[Any]] = {
        "lookup_subjects": {
            value
            for value in ids
            if {actor, wildcard}
            & set(
                local.lookup_subjects(
                    resource=ObjectRef(resource_type, value),
                    action=action,
                    subject_type="auth/user",
                )
            )
        },
        "grants_all": {"all"}
        if local.grants_all(subject=actor, action=action, resource_type=resource_type)
        else set(),
    }
    return found


def proposals(resource_type: str) -> list[dict[str, list[SubjectRef]]]:
    """Relations a proposed row of ``resource_type`` could carry, for ``check_new``."""
    if resource_type == "scope/message":
        return [
            {"cited": [SubjectRef.of("scope/profile", "1")]},
            {"cited": [SubjectRef.of("scope/thread", "1")]},
            {"cited": [SubjectRef.of("scope/thread", "2"), SubjectRef.of("scope/profile", "3")]},
        ]
    if resource_type == "scope/part":
        return [{"thread": [SubjectRef.of("scope/thread", "1")]}]
    return []


def assert_narrowed_within_published(
    local: Any,
    models: tuple[tuple[Any, str], ...],
    label: Any,
    settings: Any,
    actions: tuple[str, ...] = ("read", "hold"),
    actors: tuple[SubjectRef, ...] = (MEMBER, ADMIN),
) -> int:
    """Every decision of the narrowed policy is one of the published policy's; returns the rows lost."""
    cases = [
        (actor, model, resource_type, action)
        for actor in actors
        for model, resource_type in models
        for action in actions
    ]

    def everything() -> dict[Any, Any]:
        found = {}
        for case in cases:
            actor, _model, resource_type, action = case
            found[case] = (
                decisions(local, *case),
                lesser_paths(local, *case),
                {
                    index
                    for index, relationships in enumerate(proposals(resource_type))
                    if check_new(
                        subject=actor,
                        action=action,
                        resource_type=resource_type,
                        relationships=relationships,
                    ).allowed
                },
            )
        return found

    settings.REBAC_INHERIT_THROUGH_LINKS = True
    whole = everything()
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    narrowed = everything()
    with evaluator_scope():
        # In a scope statements are compiled for the actor's grant profile (proposal 0019).
        profiled = {case: listed(*case) for case in cases}
    lost = 0
    for case in cases:
        name = (label, str(case[0]), case[2], case[3])
        (paths, lesser, created), (narrow, narrow_lesser, narrow_created) = (
            whole[case],
            narrowed[case],
        )
        narrow["profiled"] = profiled[case]
        # Each policy is one policy on every path.
        assert len({frozenset(ids) for ids in paths.values()}) == 1, (name, paths)
        assert len({frozenset(ids) for ids in narrow.values()}) == 1, (name, narrow)
        assert narrow["list"] <= paths["list"], (name, paths, narrow)
        for path, values in narrow_lesser.items():
            assert values <= lesser[path], (name, path, lesser[path], values)
        assert narrow_created <= created, (name, "check_new", created, narrow_created)
        lost += len(paths["list"] - narrow["list"])
    return lost


def test_most_random_policies_are_narrowed():
    """The random families exercise the narrowing, not only its absence."""
    narrowed = [
        render_zed(links.narrowed(parse_zed(text))) != render_zed(parse_zed(text))
        for text in [linked(seed)[0] for seed in range(60)]
    ]
    assert sum(narrowed) >= 50


SCOPE_MODELS = (
    (ScopeThread, "scope/thread"),
    (ScopeProfile, "scope/profile"),
    (ScopeMessage, "scope/message"),
    (ScopePart, "scope/part"),
)

# The small representative case the default suite runs of each family: one seed in one
# layout, the member, the model that reaches the most links, every path, deciding ``read``.
# The slow tier runs the rest: both actors, every model, both layouts, ``read`` and ``hold``.
SCOPE_FAST = (4, "denormalized")
WIDE_FAST = (7, "denormalized")


def cases(count: int, fast: tuple[int, str]) -> list[Any]:
    return [
        (seed, storage)
        if (seed, storage) == fast
        else pytest.param(seed, storage, marks=pytest.mark.slow)
        for seed in range(count)
        for storage in ("denormalized", "registry")
    ]


@contextmanager
def mechanism(name: str, settings: Any) -> Iterator[None]:
    if name == "walk":
        settings.REBAC_BOUNDED_WALK = "decide"
    elif name == "rows":
        settings.REBAC_ROW_SCOPE = True
    yield


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize(("seed", "storage"), cases(60, SCOPE_FAST))
def test_the_narrowed_policy_grants_a_subset_on_random_policies(seed, storage, settings):
    """For every actor and object, on every read path, ``read`` and the ``hold`` it names.

    Odd seeds store the policy and add random overrides (extend, disable, tighten;
    untimed, ahead or passed); seeds divisible by three read with the bounded walk or the
    row scope.
    """
    schema, tuples, labels = linked(seed)
    rng = random.Random(f"overrides:{seed}")
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    if seed % 2:
        permissions = store(schema)
        local = backend()
        atoms = {
            "scope/thread": ["reader", "squad->member", "sibling->hold"],
            "scope/profile": ["direct", "thread->read", "pinned->read"],
            "scope/message": ["direct", "profile->read", "cited->hold", "cited->seen"],
            "scope/part": ["direct", "thread->read", "lead->read"],
        }
        random_overrides(rng, permissions, atoms)
    else:
        local = install_schema(schema)
    build_rows()
    for pk, label in labels.items():
        ScopeThread._base_manager.filter(pk=pk).update(label=label)
    local.write_relationships(tuples)
    chosen = ("walk", "rows")[seed // 3 % 2] if seed % 3 == 0 else "none"
    with mechanism(chosen, settings):
        if (seed, storage) == SCOPE_FAST:
            models = (SCOPE_MODELS[3],)
            assert_narrowed_within_published(
                local, models, (seed, storage), settings, ("read",), (MEMBER,)
            )
        else:
            assert_narrowed_within_published(local, SCOPE_MODELS, (seed, storage), settings)


def wide_policy(seed: int) -> str:
    """Folders in folders, posts with a folder and collections of folders (many-to-many),
    shelves and the attachments that name them (generic relations both ways), a hierarchy
    of multi-table branches over their parent table, and notes linked to branches and
    posts (a stored relation to two types)."""
    rng = random.Random(seed)
    shaped = shapes(rng)
    folder = ["reader", "squad->member", "admin->member"]
    post = ["direct", "folder->read", "collections->read", "pinned->read", "pinned->seen"]
    shelf = ["reader", "squad->member", "attachments->own"]
    attachment = ["reader", "target->read", "target->hold"]
    resource = ["editor", "team->member", "admin->member"]
    branch = ["manager", "resource->read", "parent->hold"]
    note = ["direct", "branch->read", "linked->read", "linked->hold"]
    return f"""
definition auth/user {{}}
definition scope/role {{ relation member: auth/user }}
definition blog/folder {{
    relation admin: scope/role // rebac:const=admin
    relation squad: scope/role
    relation parent: blog/folder // rebac:field=parent
    relation within: blog/folder
    relation reader: auth/user | auth/user:*
    relation blocked: auth/user
    permission hold = {shaped(folder, ["blocked"], simple=True)}
    permission seen = {shaped(["reader", "within->hold"], ["blocked"])}
    permission read = {shaped([*folder, "within->hold", "parent->hold", "hold"], ["blocked", "hold"])}
}}
definition test/shelf {{
    relation reader: auth/user
    relation squad: scope/role
    relation within: test/shelf
    relation seen: auth/user
    relation attachments: test/attachment // rebac:field=attachments
    permission hold = {shaped(shelf, ["within->seen"], simple=True)}
    permission read = {shaped([*shelf, "within->hold", "hold"], ["hold"])}
}}
definition test/attachment {{
    relation target: test/shelf // rebac:field=target
    relation reader: auth/user
    permission own = reader
    permission hold = {shaped(attachment, ["target->hold"], simple=True)}
    permission read = {shaped([*attachment, "hold"], ["target->hold", "hold"])}
}}
definition blog/post {{
    relation folder: blog/folder // rebac:field=folder
    relation collections: blog/folder // rebac:field=collections
    relation pinned: blog/folder | test/shelf
    relation direct: auth/user
    relation banned: auth/user
    permission hold = {shaped(["direct", "folder->read", "collections->hold"], ["banned", "collections->read"], simple=True)}
    permission read = {shaped([*post, "hold"], ["banned", "pinned->hold", "hold"])}
}}
definition test/nativeparentlinkedresource {{
    relation admin: scope/role // rebac:const=admin
    relation editor: auth/user
    relation banned: auth/user
    relation team: scope/role
    relation sibling: test/nativeparentlinkedresource
    permission hold = {shaped(resource, ["banned"], simple=True)}
    permission read = {shaped([*resource, "sibling->hold", "hold"], ["banned", "hold"])}
}}
definition test/nativeparentlinkedbranch {{
    relation resource: test/nativeparentlinkedresource // rebac:field=nativeparentlinkedresource_ptr
    relation parent: test/nativeparentlinkedbranch // rebac:field=parent
    relation manager: auth/user
    permission hold = {shaped(["manager", "resource->hold"], ["resource->read"], simple=True)}
    permission read = {shaped([*branch, "hold"], ["resource->hold", "hold"])}
}}
definition test/nativeparentlinkednote {{
    relation branch: test/nativeparentlinkedbranch // rebac:field=branch
    relation linked: test/nativeparentlinkedbranch | blog/post
    relation direct: auth/user
    permission hold = {shaped(note, ["linked->hold"], simple=True)}
    permission read = {shaped([*note, "hold"], ["linked->hold", "branch->hold", "hold"])}
}}
"""


def build_wide(rng: random.Random) -> list[RelationshipTuple]:
    """The rows of the wide family, and random relationship rows over them."""
    with sudo(reason="test.fixture"):
        folders = [Folder.objects.create(name="f1")]
        folders.append(Folder.objects.create(name="f2", parent=folders[0]))
        folders.append(Folder.objects.create(name="f3", parent=folders[1]))
        posts = [
            Post.objects.create(title="p1", folder=folders[0]),
            Post.objects.create(title="p2", folder=folders[2]),
        ]
        posts[0].collections.add(folders[1])
        posts[1].collections.add(folders[0])
        shelves = [Shelf.objects.create(name="s1"), Shelf.objects.create(name="s2")]
        kind = ContentType.objects.get_for_model(Shelf)
        attachments = [
            Attachment.objects.create(content_type=kind, object_id=shelf.pk)
            for shelf in (shelves[0], shelves[1], shelves[0])
        ]
        branches = [NativeParentLinkedBranch.objects.create(name="b1")]
        branches.append(NativeParentLinkedBranch.objects.create(name="b2", parent=branches[0]))
        notes = [
            NativeParentLinkedNote.objects.create(branch=branches[0]),
            NativeParentLinkedNote.objects.create(branch=branches[1]),
        ]
    actors = (MEMBER, ADMIN)
    squad = SubjectRef.of("scope/role", "squad")

    def each(type_: str, objects: list[Any], *relations: str) -> list[RelationshipTuple]:
        return [
            grant(type_, obj.pk, relation, actor)
            for obj in objects
            for relation in relations
            for actor in actors
        ]

    candidates = [
        grant("scope/role", "admin", "member", ADMIN),
        *each("blog/folder", folders, "reader", "blocked"),
        grant("blog/folder", folders[2].pk, "reader", EVERYONE),
        grant("blog/folder", folders[1].pk, "squad", squad),
        grant("blog/folder", folders[0].pk, "within", SubjectRef.of("blog/folder", str(folders[2].pk))),
        *each("blog/post", posts, "direct", "banned"),
        grant("blog/post", posts[0].pk, "pinned", SubjectRef.of("blog/folder", str(folders[2].pk))),
        grant("blog/post", posts[1].pk, "pinned", SubjectRef.of("test/shelf", str(shelves[1].pk))),
        *each("test/shelf", shelves, "reader", "seen"),
        grant("test/shelf", shelves[0].pk, "squad", squad),
        grant("test/shelf", shelves[1].pk, "within", SubjectRef.of("test/shelf", str(shelves[0].pk))),
        *each("test/attachment", attachments, "reader"),
        *each("test/nativeparentlinkedresource", branches, "editor", "banned"),
        grant("test/nativeparentlinkedresource", branches[1].pk, "team", squad),
        grant(
            "test/nativeparentlinkedresource",
            branches[1].pk,
            "sibling",
            SubjectRef.of("test/nativeparentlinkedresource", str(branches[0].pk)),
        ),
        *each("test/nativeparentlinkedbranch", branches, "manager"),
        *each("test/nativeparentlinkednote", notes, "direct"),
        grant("test/nativeparentlinkednote", notes[0].pk, "linked", SubjectRef.of("test/nativeparentlinkedbranch", str(branches[1].pk))),
        grant("test/nativeparentlinkednote", notes[1].pk, "linked", SubjectRef.of("blog/post", str(posts[0].pk))),
    ]  # fmt: skip
    always = [grant("scope/role", "squad", "member")]
    return always + [item for item in candidates if rng.random() < 0.45]


WIDE_MODELS = (
    (Folder, "blog/folder"),
    (Post, "blog/post"),
    (Shelf, "test/shelf"),
    (Attachment, "test/attachment"),
    (NativeParentLinkedResource, "test/nativeparentlinkedresource"),
    (NativeParentLinkedBranch, "test/nativeparentlinkedbranch"),
    (NativeParentLinkedNote, "test/nativeparentlinkednote"),
)


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize(("seed", "storage"), cases(40, WIDE_FAST))
def test_the_narrowed_policy_grants_a_subset_on_random_wide_policies(seed, storage, settings):
    """Same-type relations, a many-to-many, generic relations both ways, the multi-table
    parent link in a hierarchy, stored relations to two types, wildcards; every read path."""
    schema = wide_policy(seed)
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(schema)
    tuples = build_wide(random.Random(f"rows:{seed}"))
    local.write_relationships(tuples)
    if (seed, storage) == WIDE_FAST:
        models = (WIDE_MODELS[6],)
        assert_narrowed_within_published(
            local, models, (seed, storage), settings, ("read",), (MEMBER,)
        )
    else:
        assert_narrowed_within_published(local, WIDE_MODELS, (seed, storage), settings)


# ---------- Names a policy can spell ----------

UNSPELLED = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation sibling: scope/thread
    permission read = reader + sibling->read
}
definition scope/profile {
    relation direct: auth/user
    permission other = direct
}
definition scope/message {
    relation linked: scope/thread | scope/profile
    permission read = linked->read
}
"""


def with_permission(schema: Schema, type_: str, permission: Permission) -> Schema:
    definitions = [
        d
        if d.resource_type != type_
        else Definition(d.resource_type, d.relations, (*d.permissions, permission))
        for d in schema.definitions
    ]
    return Schema(definitions=definitions)


def unspelled(case: str) -> Schema:
    """A schema built in code with a name the schema language cannot spell."""
    schema = parse_zed(UNSPELLED)
    if case == "a generated name on a type without the target":
        return with_permission(schema, "scope/profile", Permission("lent(read)", PermRef("direct")))
    if case == "a generated name beside the target":
        return with_permission(schema, "scope/thread", Permission("lent(read)", PermRef("reader")))
    if case == "a generated name a reference reaches":
        # ``lent(read) = reader + lent(near)``: ``lent(near)`` is generated on the type itself.
        nearer = parse_zed(
            UNSPELLED.replace(
                "permission read = reader + sibling->read",
                "permission near = sibling->read\n    permission read = reader + near",
            )
        )
        return with_permission(nearer, "scope/thread", Permission("lent(near)", PermRef("reader")))
    if case == "an arrow to a generated name":
        return with_permission(
            schema, "scope/message", Permission("seen", PermArrow("linked", "lent(read)"))
        )
    if case == "a reference with a space":
        return with_permission(schema, "scope/thread", Permission("seen", PermRef("rea der")))
    if case == "a relation through Definition.extend":
        thread = schema.get_definition("scope/thread")
        assert thread is not None
        extended = thread.extend(relations=[Relation("not-a-name", (AllowedSubject("auth/user"),))])
        return Schema(definitions=[extended if d is thread else d for d in schema.definitions])
    assert case == "a definition type"
    return Schema(definitions=[*schema.definitions, Definition("bad type", (), ())])


UNSPELLED_CASES = [
    "a generated name on a type without the target",
    "a generated name beside the target",
    "a generated name a reference reaches",
    "an arrow to a generated name",
    "a reference with a space",
    "a relation through Definition.extend",
    "a definition type",
]


@pytest.mark.parametrize("case", UNSPELLED_CASES)
def test_a_schema_must_spell_its_names_whoever_built_it(case):
    from rebac.backends.local import LocalBackend
    from rebac.schema.parser import name_errors, validate_schema

    schema = unspelled(case)
    assert name_errors(schema)
    assert set(name_errors(schema)) <= set(validate_schema(schema))
    with pytest.raises(SchemaError, match="not"):
        LocalBackend().set_schema(schema)
    assert name_errors(parse_zed(SMALL)) == []


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("same_type", [False, True], ids=["other type", "same type"])
def test_a_programmatic_declared_generated_name_is_refused(same_type):
    """The reviewer's case: ``lent(read)`` added in code, so that the narrowed arrow
    ``linked->lent(read)`` would evaluate it. No such schema is installed."""
    case = "a generated name beside the target" if same_type else UNSPELLED_CASES[0]
    with pytest.raises(SchemaError, match=r"'lent\(read\)' is not a name"):
        install_schema(unspelled(case))


@pytest.mark.django_db
def test_a_stored_name_the_language_cannot_spell_is_refused_at_its_write():
    from rebac.models import SchemaDefinition, SchemaPermission
    from rebac.models.schema_write import schema_index_write

    reset_backend()
    with pytest.raises(SchemaError, match=r"'lent\(read\)' is not a name"):
        with schema_index_write("default"):
            definition = SchemaDefinition.objects.create(resource_type="scope/thread")
            SchemaPermission.objects.create(
                definition=definition, name="lent(read)", expression="nil"
            )
    assert not SchemaPermission.objects.filter(name="lent(read)").exists()


@pytest.mark.parametrize(
    ("case", "expression"),
    [
        ("nil as a reference", PermRef("nil")),
        ("a slash in a reference", PermRef("scope/reader")),
        ("a slash in an arrow's relation", PermArrow("scope/sibling", "read")),
        ("nil as an arrow's relation", PermArrow("nil", "read")),
    ],
)
def test_a_reference_names_a_relation_or_a_permission(case, expression):
    """The parser reads ``nil`` as the empty set, and nothing with a slash can be declared."""
    from rebac.schema.parser import name_errors

    schema = with_permission(parse_zed(UNSPELLED), "scope/thread", Permission("seen", expression))
    assert name_errors(schema), case


def test_a_caveat_named_expiration_cannot_be_referenced():
    """``with expiration`` is the expiration modifier, never a caveat."""
    from rebac.schema.parser import name_errors

    schema = parse_zed(UNSPELLED)
    thread = schema.get_definition("scope/thread")
    assert thread is not None
    expiring = thread.extend(
        relations=[Relation("timed", (AllowedSubject("auth/user", with_caveat="expiration"),))]
    )
    schema = Schema(definitions=[expiring if d is thread else d for d in schema.definitions])
    assert any("'expiration'" in error for error in name_errors(schema))


def test_a_name_error_is_reported_once():
    from rebac.backends.local import LocalBackend

    schema = parse_zed(UNSPELLED)
    thread = schema.get_definition("scope/thread")
    assert thread is not None
    named = thread.extend(relations=[Relation("backing-x", (AllowedSubject("auth/user"),))])
    schema = Schema(definitions=[named if d is thread else d for d in schema.definitions])
    with pytest.raises(SchemaError) as raised:
        LocalBackend().set_schema(schema)
    assert str(raised.value).count("'backing-x' is not a name") == 1


# A stored policy written before names were checked: it keeps deciding.


@pytest.fixture
def unspelled_stored(settings):
    """The reviewer's case: a stored permission renamed outside the write owners, as code
    that wrote rows directly could have, to a name no ``.zed`` text can spell; then a new
    revision is published."""
    from rebac.models import SchemaPermission
    from rebac.models.generation import SchemaGeneration

    permissions = stored_policy(settings, "denormalized", message="direct + linked->read")
    build_rows()
    backend().write_relationships(
        [grant("scope/message", 1, "direct"), grant("scope/thread", 1, "reader")]
    )
    meta = SchemaPermission._meta
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET name = %s "
            f"WHERE {connection.ops.quote_name(meta.pk.column)} = %s",
            ["can-read", permissions[("scope/message", "read")].pk],
        )
    SchemaGeneration.objects.filter(pk=1).update(revision="after-upgrade")
    backend().mark_schema_stale()
    return permissions


def stored_name_issues(databases: list[str] | None) -> list[Any]:
    from django.core.checks import run_checks

    return [issue for issue in run_checks(databases=databases) if issue.id == "rebac.W013"]


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_stored_policy_holding_an_unspelled_name_keeps_deciding(unspelled_stored):
    thread, message = ObjectRef("scope/thread", "1"), ObjectRef("scope/message", "1")
    assert backend().check_access(subject=MEMBER, action="read", resource=thread).allowed
    assert backend().check_access(subject=MEMBER, action="can-read", resource=message).allowed
    assert not backend().check_access(subject=MEMBER, action="read", resource=message).allowed
    assert readable(ScopeThread) == {1}
    backend().write_relationships([grant("scope/message", 2, "direct")])
    assert (
        backend()
        .check_access(subject=MEMBER, action="can-read", resource=ObjectRef("scope/message", "2"))
        .allowed
    )
    # The checks run; only the database checks read the stored policy's names.
    assert stored_name_issues(None) == []
    [issue] = stored_name_issues(["default"])
    assert "scope/message: permission 'can-read' is not a name" in issue.msg
    assert "Rename it through the schema write API" in issue.hint
    # ``rebac sync`` reads it: the drift from the test's .zed files, not a refusal.
    with pytest.raises(CommandError, match="Schema drift detected"):
        call_command("rebac", "sync", "--check", stdout=StringIO(), stderr=StringIO())


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_an_unspelled_stored_name_is_a_warning_that_never_stops_migrate(
    unspelled_stored, settings, monkeypatch
):
    """``migrate`` runs the database checks and stops at an error: a policy that decides as it
    did before names were checked must not stop an upgrade. ``check --database`` and
    ``migrate`` report the warning and proceed; it is silenced like the library's others."""
    from django.core.checks import WARNING
    from django.core.management.base import SystemCheckError
    from django.test.utils import override_system_checks

    from rebac import checks as system_checks

    [issue] = stored_name_issues(["default"])
    assert issue.level == WARNING
    assert "SILENCED_SYSTEM_CHECKS = ['rebac.W013']" in issue.hint
    # The test project carries models the database check refuses on purpose (rebac.E014):
    # the commands run that check alone, over the models this policy names.
    scoped = {ScopeThread, ScopeMessage, ScopePart, ScopeProfile}
    monkeypatch.setattr(system_checks, "_is_rebac_bound", lambda model: model in scoped)
    with override_system_checks([system_checks.check_policy_models]):
        # ``call_command`` skips a command's checks unless told not to; ``check`` runs them.
        for command, options in (
            (("check", "--database", "default"), {}),
            (("migrate", "rebac"), {"skip_checks": False}),
        ):
            err = StringIO()
            call_command(*command, **options, stdout=StringIO(), stderr=err)
            assert "(rebac.W013)" in err.getvalue(), command
        # A warning, not an error: a stricter level stops at it, the default does not.
        with pytest.raises(SystemCheckError, match=r"rebac\.W013"):
            call_command(
                "check", "--database", "default", "--fail-level", "WARNING", stdout=StringIO()
            )
        settings.SILENCED_SYSTEM_CHECKS = ["rebac.W013"]
        assert issue.is_silenced()
        call_command("check", "--database", "default", "--fail-level", "WARNING", stdout=StringIO())


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_an_unspelled_stored_name_is_repaired_through_the_write_api(unspelled_stored):
    from rebac.models import SchemaPermission

    message = ObjectRef("scope/message", "1")
    SchemaPermission.objects.filter(name="can-read").update(name="read")
    assert backend().check_access(subject=MEMBER, action="read", resource=message).allowed
    assert stored_name_issues(["default"]) == []


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_write_may_not_add_an_unspelled_name_beside_an_old_one(unspelled_stored):
    from rebac import schema_changes
    from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation

    thread = SchemaDefinition.objects.get(resource_type="scope/thread")
    # An unrelated write beside the stored name.
    SchemaRelation.objects.create(
        definition=thread, name="watcher", allowed_subjects=[{"type": "auth/user"}]
    )
    with pytest.raises(
        SchemaError,
        match=r"'can-write' is not a name: a write may not add .* cannot spell \(rebac\.W013\)",
    ):
        SchemaPermission.objects.create(definition=thread, name="can-write", expression="reader")
    assert not SchemaPermission.objects.filter(name="can-write").exists()
    # A second place for an old name is added too; a repair in the same block is not.
    with pytest.raises(SchemaError, match=r"scope/thread: permission 'can-read'"):
        with schema_changes():
            SchemaPermission.objects.filter(name="can-read").update(name="read")
            SchemaPermission.objects.create(definition=thread, name="can-read", expression="nil")
    with schema_changes():
        SchemaPermission.objects.filter(name="can-read").update(name="read")
        SchemaPermission.objects.create(definition=thread, name="seen", expression="reader")
    assert stored_name_issues(["default"]) == []


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_without_inheritance_through_links_an_unspelled_stored_name_fails_closed(
    unspelled_stored, settings
):
    from rebac.checks import check_inherit_through_links

    settings.REBAC_INHERIT_THROUGH_LINKS = False
    for _ in range(2):
        with pytest.raises(links.NarrowingError, match=r"'can-read' is not a name") as raised:
            backend().check_access(
                subject=MEMBER, action="read", resource=ObjectRef("scope/thread", "1")
            )
        assert raised.value.kept
    with pytest.raises(links.NarrowingError, match=r"rebac\.E025"):
        readable(ScopeMessage, "can-read")
    backend().write_relationships([grant("scope/message", 2, "direct")])
    issues = {issue.id: issue.msg for issue in check_inherit_through_links()}
    assert set(issues) == {"rebac.W012", "rebac.E025"}
    assert "'can-read'" in issues["rebac.E025"]


# ---------- Names read one way ----------

# The reviewer's policy: a thread's relation and permission both named ``x``. The compiler reads
# the relation; the narrowing versioned the permission, and narrowed the member read message 1,
# which the published policy denies.
SAME_NAME = """
definition auth/user {}
definition scope/profile {
    relation reader: auth/user
}
definition scope/thread {
    relation x: auth/user
    relation everyone: auth/user
    relation keeper: scope/profile
    permission x = everyone + keeper->reader
    permission read = x
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread
    relation direct: auth/user
    permission read = direct + linked->x
}
"""


@pytest.fixture
def two_ways_stored(settings):
    """The reviewer's policy as a stored policy from before the rule was held at runtime: stored
    with the permission named ``xperm``, then renamed ``x`` outside the write owners. The member
    is in thread 1's ``everyone``; message 1 is linked to thread 1."""
    from rebac.models import SchemaPermission
    from rebac.models.generation import SchemaGeneration

    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    found = store(SAME_NAME.replace("permission x = ", "permission xperm = "))
    build_rows()
    backend().write_relationships(
        [
            grant("scope/thread", 1, "everyone"),
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
        ]
    )
    meta = SchemaPermission._meta
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET name = %s "
            f"WHERE {connection.ops.quote_name(meta.pk.column)} = %s",
            ["x", found[("scope/thread", "xperm")].pk],
        )
    SchemaGeneration.objects.filter(pk=1).update(revision="after-upgrade")
    backend().mark_schema_stale()


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_stored_policy_that_reads_a_name_two_ways_keeps_deciding(two_ways_stored, settings):
    """It decides as it did: the relation ``x``, which nobody holds. The database check warns
    (``rebac.W014``), and the narrowing refuses it, kept, so with the setting ``False`` every
    decision fails closed where it read message 1, which the published policy denies."""
    from django.core.checks import WARNING, run_checks

    assert readable(ScopeMessage) == set()
    assert (
        not backend()
        .check_access(subject=MEMBER, action="read", resource=ObjectRef("scope/message", "1"))
        .allowed
    )
    [issue] = [i for i in run_checks(databases=["default"]) if i.id == "rebac.W014"]
    assert issue.level == WARNING and "scope/thread#x: name collision" in issue.msg
    assert [i for i in run_checks() if i.id == "rebac.W014"] == []
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    for _ in range(2):
        with pytest.raises(links.NarrowingError, match=r"name collision.*rebac\.E025") as raised:
            readable(ScopeMessage)
        assert raised.value.kept
    # A write beside it proceeds; a repair clears the warning.
    backend().write_relationships([grant("scope/message", 2, "direct")])
    settings.REBAC_INHERIT_THROUGH_LINKS = True
    from rebac.models import SchemaPermission

    SchemaPermission.objects.filter(definition__resource_type="scope/thread", name="x").update(
        name="xperm"
    )
    assert [i for i in run_checks(databases=["default"]) if i.id == "rebac.W014"] == []


def test_the_reviewers_policy_under_the_narrowing_is_refused(monkeypatch):
    """Installed past the runtime check: the narrowing refuses it rather than version the
    permission ``x`` where the compiler reads the relation."""
    from rebac.backends import local as local_module

    monkeypatch.setattr(local_module, "_name_conflicts", lambda schema: [])
    with pytest.raises(links.NarrowingError, match="name collision") as raised:
        links.narrowed(parse_zed(SAME_NAME))
    assert raised.value.kept


ARROW_TO_A_GENERATED_NAME = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation sibling: scope/thread
    permission read = reader + sibling->read
}
definition scope/message {
    relation linked: scope/thread
    permission read = linked->read
    permission other = linked->read
}
"""


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_the_narrowing_checks_the_names_of_its_own_input(settings, monkeypatch):
    """With the installation's check bypassed, a published reference ``linked->lent(read)``
    would evaluate the ``lent(read)`` the narrowing generates for ``other``: the narrowing
    refuses the policy itself."""
    from rebac.backends import local as local_module

    monkeypatch.setattr(local_module, "_spelling_errors", lambda schema: [])
    schema = parse_zed(ARROW_TO_A_GENERATED_NAME)
    local = install_schema(
        with_permission(
            Schema(
                definitions=[
                    d
                    if d.resource_type != "scope/message"
                    else Definition(
                        d.resource_type,
                        d.relations,
                        tuple(p for p in d.permissions if p.name != "read"),
                    )
                    for d in schema.definitions
                ]
            ),
            "scope/message",
            Permission("read", PermArrow("linked", "lent(read)")),
        )
    )
    build_rows()
    local.write_relationships(
        [
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
            grant("scope/thread", 1, "reader"),
        ]
    )
    published = readable(ScopeMessage)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    with pytest.raises(
        links.NarrowingError, match=r"linked->lent\(read\) is not an arrow"
    ) as raised:
        readable(ScopeMessage)
    assert raised.value.kept
    settings.REBAC_INHERIT_THROUGH_LINKS = True
    assert readable(ScopeMessage) == published == set()


def test_the_narrowing_refuses_a_policy_with_a_name_it_cannot_spell():
    """Defence beside the schema's own check: the narrowing never emits a name the
    composed policy holds, nor two permissions of one name. A refusal is kept."""
    for case in UNSPELLED_CASES[:4]:
        with pytest.raises(links.NarrowingError, match=r"rebac\.E025") as raised:
            links.narrowed(unspelled(case))
        assert raised.value.kept


def unchecked(monkeypatch: Any) -> None:
    """Names are not checked: what the narrowing's own refusal is left to catch."""
    from rebac.schema import parser

    monkeypatch.setattr(parser, "name_errors", lambda schema: [])


@pytest.mark.parametrize("case", UNSPELLED_CASES[:3])
def test_without_the_names_check_a_generated_name_is_never_emitted_over_a_declared_one(
    case, monkeypatch
):
    """The arrow ``linked->read`` is narrowed to ``linked->lent(read)``, which reaches both
    subject types of ``linked``: neither may declare ``lent(read)``; nor may a type declare
    the ``lent(near)`` its own ``lent(read)`` refers to."""
    unchecked(monkeypatch)
    with pytest.raises(links.NarrowingError, match=r"lent\((read|near)\) is declared") as raised:
        links.narrowed(unspelled(case))
    assert raised.value.kept


def test_two_permissions_of_one_name_are_refused():
    schema = parse_zed(SMALL)
    thread = schema.get_definition("scope/thread")
    assert thread is not None
    doubled = Definition(
        thread.resource_type, thread.relations, (*thread.permissions, thread.permissions[0])
    )
    with pytest.raises(links.NarrowingError, match="duplicate permission name") as raised:
        links.narrowed(
            Schema(definitions=[doubled if d is thread else d for d in schema.definitions])
        )
    assert raised.value.kept


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("same_type", [False, True], ids=["other type", "same type"])
def test_a_programmatic_declared_generated_name(settings, monkeypatch, same_type):
    """The reviewer's case, installed past the names check: reads fail closed with the
    setting ``False``, the published policy is unchanged, and the check reports it."""
    from rebac.checks import check_inherit_through_links

    unchecked(monkeypatch)
    target = "scope/thread" if same_type else "scope/profile"
    relation = "reader" if same_type else "direct"
    local = install_schema(
        unspelled(
            "a generated name beside the target"
            if same_type
            else "a generated name on a type without the target"
        )
    )
    build_rows()
    local.write_relationships(
        [
            grant("scope/message", 1, "linked", SubjectRef.of(target, "1")),
            grant(target, 1, relation),
        ]
    )
    published = readable(ScopeMessage)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    with pytest.raises(links.NarrowingError, match=r"is declared"):
        readable(ScopeMessage)
    with pytest.raises(links.NarrowingError, match=r"is declared"):
        local.check_access(subject=MEMBER, action="read", resource=ObjectRef("scope/message", "1"))
    assert "rebac.E025" in {issue.id for issue in check_inherit_through_links()}
    settings.REBAC_INHERIT_THROUGH_LINKS = True
    assert readable(ScopeMessage) == published


# ---------- A narrowing failure ----------


@pytest.mark.django_db
def test_an_unexpected_failure_is_raised_and_not_kept(rows, settings, monkeypatch):
    calls = {"classify": 0}
    real = links.classify

    def flaky(definition: Any, relation: Any) -> Any:
        calls["classify"] += 1
        if calls["classify"] == 1:
            raise RuntimeError("transient")
        return real(definition, relation)

    monkeypatch.setattr(links, "classify", flaky)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    rows.write_relationships(THROUGH_SIBLING)
    with pytest.raises(links.NarrowingError) as raised:
        readable(ScopePart, "via_thread")
    assert not raised.value.kept
    # The next operation narrows again, and succeeds.
    assert readable(ScopePart, "via_thread") == {3}


@pytest.mark.django_db
def test_a_refusal_is_kept_and_reported_by_the_check_for_that_policy(rows, settings, monkeypatch):
    from rebac.checks import check_inherit_through_links

    calls = {"refusals": 0}

    def refused(schema: Schema) -> list[str]:
        calls["refusals"] += 1
        return ["scope/thread: a name for the test"]

    monkeypatch.setattr(links, "_spelling_errors", refused)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    for _ in range(3):
        with pytest.raises(links.NarrowingError, match=r"a name for the test") as raised:
            readable(ScopeMessage)
        assert raised.value.kept
    assert calls["refusals"] == 1
    issues = {issue.id: issue.msg for issue in check_inherit_through_links()}
    assert set(issues) == {"rebac.W012", "rebac.E025"}
    assert "a name for the test" in issues["rebac.E025"]


# ---------- One source for check_new ----------


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("kind", ["an expired arm", "a disable"])
def test_check_new_reads_the_policy_the_other_paths_read(settings, kind):
    """``check_new`` walks the evaluated policy in Python: the narrowed composition of the
    overrides active now, the same object the compiled policy's checks fall back to."""
    from rebac.compile import read

    base = "direct" if kind == "an expired arm" else "direct + linked->read"
    permissions = stored_policy(settings, "denormalized", message=base)
    build_rows()
    backend().write_relationships([grant("scope/thread", 1, "reader")])
    if kind == "an expired arm":
        override(
            permissions[("scope/message", "read")],
            "extend",
            "linked->read",
            expires_at=timezone.now() - timedelta(minutes=5),
        )
    else:
        override(permissions[("scope/thread", "read")], "disable", "reader")
    proposed = {"linked": [SubjectRef.of("scope/thread", "1")]}
    for value in (True, False):
        settings.REBAC_INHERIT_THROUGH_LINKS = value
        found = check_new(
            subject=MEMBER, action="read", resource_type="scope/message", relationships=proposed
        )
        # Published: the arm has expired, or the thread's reader is disabled.
        assert not found.allowed, (kind, value)
    assert backend().evaluated_schema() is read._policy(backend()).effective


# ---------- What a caller can obtain ----------


@pytest.mark.django_db
def test_public_names_obtain_the_published_policy(rows, settings):
    from rebac.roles import roles_reaching

    settings.REBAC_INHERIT_THROUGH_LINKS = False
    names = {p.name for d in rows.schema().definitions for p in d.permissions}
    assert not any(links.is_generated(name) for name in names)
    assert rows.schema() is rows._published_schema()
    evaluated = {p.name for d in rows.evaluated_schema().definitions for p in d.permissions}
    assert "lent(read)" in evaluated
    assert roles_reaching("scope/thread", "lent(read)", role_resource_type="scope/role") == set()
    assert roles_reaching(
        "scope/thread", "read", role_resource_type="scope/role"
    ) == roles_reaching(
        "scope/thread", "read", role_resource_type="scope/role", schema=rows.schema()
    )
    assert "published_for" not in links.__all__ and not hasattr(links, "published_for")


# A message reads its thread's read (a parent) and lends only ``direct``: the narrowed policy
# holds ``lent(read)`` on a message.
LENDING = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation linked: scope/message
    permission read = reader + linked->read
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = direct + thread->read
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->read
}
"""


@pytest.mark.django_db
def test_the_experimental_entries_refuse_a_generated_name(settings):
    """``rebac.structural.edges()`` lists no edge under a generated name, ``explain`` says it is
    one, as parent's action or child's, and ``rebac.display.admits_reading`` admits no row
    under one, though the narrowed policy holds it and would prove it."""
    from rebac import display, structural

    local = install_schema(LENDING)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    settings.REBAC_DISPLAY_FIELDS = True
    assert local.evaluated_schema().get_permission("scope/message", "lent(read)") is not None
    names = {name for edge in structural.edges() for name in edge.parent_actions}
    assert "read" in names and not any(links.is_generated(name) for name in names)
    edge = {"child_model": ScopePart, "fk_name": "message", "parent_model": ScopeMessage}
    assert structural.explain(**edge, child_action="read", parent_action="read") is None
    for child_action, parent_action in (("read", "lent(read)"), ("lent(read)", "read")):
        assert (
            structural.explain(**edge, child_action=child_action, parent_action=parent_action)
            == "lent(read) is a generated name, not an action"
        )
    assert display.admits_reading(ScopeMessage, "read")
    assert not display.admits_reading(ScopeMessage, "lent(read)")


# The admin role reaches a thread's read; a message reads its thread's (a parent), a part the
# read of a message it pins (a link), which a message does not lend.
REACHED_THROUGH_A_LINK = """
definition auth/user {}
definition scope/role { relation member: auth/user }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    permission read = admin->member
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->read
}
definition scope/part {
    relation pinned: scope/message
    permission read = pinned->read
}
"""


@pytest.mark.django_db
def test_roles_reaching_names_a_superset_of_the_evaluated_policys_roles(settings):
    """``roles_reaching`` reads the published policy: under ``False`` it still names a role
    reached only through a link the narrowing narrows. Given the evaluated policy, it names
    what that policy lets reach the permission."""
    from rebac.roles import roles_reaching

    local = install_schema(REACHED_THROUGH_A_LINK)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    admin = frozenset({ObjectRef("scope/role", "admin")})
    evaluated = local.evaluated_schema()
    assert roles_reaching("scope/part", "read", role_resource_type="scope/role") == admin
    assert (
        roles_reaching("scope/part", "read", role_resource_type="scope/role", schema=evaluated)
        == frozenset()
    )
    for schema in (None, evaluated):
        assert (
            roles_reaching("scope/message", "read", role_resource_type="scope/role", schema=schema)
            == admin
        )


@pytest.mark.django_db
def test_the_evaluated_policy_is_public_and_kept_per_snapshot(rows, settings, monkeypatch):
    """``evaluated_schema()``: the published policy itself with the setting ``True``; with
    ``False`` the narrowed one, the very object decisions read, one per snapshot (the same object
    at every call, and again after a flip back), another after a policy change."""
    from rebac.compile import read as compiled

    settings.REBAC_INHERIT_THROUGH_LINKS = True
    # What check_new walks; the compiled policy composes an equal copy.
    assert rows.evaluated_schema() is rows.schema() is rows._schema_snapshot().schema
    assert rows.evaluated_schema() == compiled._policy(rows).effective
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    first = rows.evaluated_schema()
    assert first is rows.evaluated_schema() is compiled._policy(rows).effective
    assert first is not rows.schema()
    assert any(links.is_generated(p.name) for d in first.definitions for p in d.permissions)
    assert not any(
        links.is_generated(p.name) for d in rows.schema().definitions for p in d.permissions
    )
    with evaluator_scope():
        assert rows.evaluated_schema() is first
    # Changed as a running process sees it, with no signal: the snapshot stays, and so does its
    # narrowing. (Django's setting_changed signal marks every loaded schema stale.)
    flip(monkeypatch, True)
    assert rows.evaluated_schema() is rows.schema()
    flip(monkeypatch, False)
    assert rows.evaluated_schema() is first
    rows.set_schema(
        parse_zed(ROWS_POLICY.replace("permission veto = blocked", "permission veto = reader"))
    )
    second = rows.evaluated_schema()
    assert second is not first and second is rows.evaluated_schema()
    assert expression(second, "scope/thread", "veto") == "reader"


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_the_evaluated_policy_of_a_stored_policy_is_kept_per_snapshot(settings, storage):
    """A stored policy: the same object across calls, also outside every scope, while the
    policy does not change; another after a write to it."""
    from rebac.compile import read as compiled

    found = stored_policy(settings, storage, message="direct + linked->read")
    local = backend()
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    first = local.evaluated_schema()
    assert first is local.evaluated_schema() is compiled._policy(local).effective
    assert expression(first, "scope/message", "read") == "(direct + linked->lent(read))"
    permission = found[("scope/message", "read")]
    permission.expression = "direct"
    permission.save()
    second = local.evaluated_schema()
    assert second is not first and second is local.evaluated_schema()
    assert expression(second, "scope/message", "read") == "direct"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "command", [("links",), ("links", "--json"), ("explain", "scope/thread.read")]
)
def test_the_commands_report_a_setting_that_is_not_a_bool_as_a_command_error(
    rows, settings, command
):
    """``rebac links`` decides nothing and ``rebac explain`` renders: under ``rebac.E028`` each
    reports the misconfiguration as a command error naming the check, not a traceback."""
    settings.REBAC_INHERIT_THROUGH_LINKS = "False"
    with pytest.raises(CommandError, match=r"REBAC_INHERIT_THROUGH_LINKS.*rebac\.E028"):
        call_command("rebac", *command, stdout=StringIO(), stderr=StringIO())
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    call_command("rebac", *command, stdout=StringIO(), stderr=StringIO())


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["False", "True", "", 0, 1, None])
def test_a_setting_that_is_not_a_bool_chooses_no_policy(rows, settings, value):
    """The string ``"False"`` an environment variable gives read as truthy: the published policy,
    silently, and no ``rebac.W012``. Only the booleans choose: any other value has no safe
    reading (``rebac.conf``), so ``rebac.E028`` reports it, the check that reads it says it did not
    run, and every decision raises until it is fixed; what decides nothing proceeds."""
    from django.core.checks import run_checks
    from django.core.exceptions import ImproperlyConfigured

    rows.write_relationships([grant("scope/message", 1, "direct")])
    settings.REBAC_INHERIT_THROUGH_LINKS = value
    issues = [issue for issue in run_checks() if issue.id.startswith("rebac.")]
    [wrong] = [
        issue
        for issue in issues
        if issue.id == "rebac.E028" and "REBAC_INHERIT_THROUGH_LINKS" in issue.msg
    ]
    assert "every read of it raises ImproperlyConfigured" in wrong.msg
    not_run = {
        issue.id for issue in issues if "check_inherit_through_links did not run" in issue.msg
    }
    assert not_run == {"rebac.W012", "rebac.E025"}
    for decide in (
        links.inherits_through_links,
        rows.evaluated_schema,
        lambda: list(ScopeMessage.objects.with_actor(MEMBER)),
        lambda: rows.check_access(
            subject=MEMBER, action="read", resource=ObjectRef("scope/message", "1")
        ),
        lambda: check_new(
            subject=MEMBER, action="read", resource_type="scope/message", relationships={}
        ),
    ):
        with pytest.raises(ImproperlyConfigured, match=r"REBAC_INHERIT_THROUGH_LINKS.*rebac\.E028"):
            decide()
    assert rows.schema() is rows._published_schema()
    rows.write_relationships([grant("scope/message", 2, "direct")])
    for chosen, expected in ((True, set()), (False, {"rebac.W012"})):
        settings.REBAC_INHERIT_THROUGH_LINKS = chosen
        reported = {issue.id for issue in run_checks() if issue.id.startswith("rebac.")}
        assert reported & {"rebac.E028", "rebac.W012"} == expected, chosen
        assert sorted(m.pk for m in ScopeMessage.objects.with_actor(MEMBER)) == [1, 2]


# ---------- The report ----------


def test_the_report_lists_each_narrowed_arrow_with_what_is_lent():
    found = links.narrowed_arrows(parse_zed(SMALL))
    assert [
        (a.resource_type, a.permission, a.relation, a.reason, a.target_types, a.target, a.lent)
        for a in found
    ] == [
        ("scope/message", "both", "profile", "a reverse relation", ("scope/profile",), "read",
         (("scope/profile", "direct"),)),
        ("scope/message", "hold", "profile", "a reverse relation", ("scope/profile",), "read",
         (("scope/profile", "direct"),)),
        ("scope/message", "lens", "profile", "a reverse relation", ("scope/profile",),
         "screened", (("scope/profile", "(direct - thread->veto)"),)),
        ("scope/message", "read", "profile", "a reverse relation", ("scope/profile",), "read",
         (("scope/profile", "direct"),)),
        ("scope/message", "twice", "profile", "a reverse relation", ("scope/profile",), "read",
         (("scope/profile", "direct"),)),
        ("scope/part", "via_thread", "thread", "a path of several hops", ("scope/thread",),
         "read", (("scope/thread", "((reader + squad->member) + admin->member)"),)),
        ("scope/profile", "read", "thread", "a path of several hops", ("scope/thread",), "read",
         (("scope/thread", "((reader + squad->member) + admin->member)"),)),
        ("scope/profile", "screened", "thread", "a path of several hops", ("scope/thread",),
         "read", (("scope/thread", "((reader + squad->member) + admin->member)"),)),
    ]  # fmt: skip
    reaches = {(a.resource_type, a.permission): a.reaches for a in found}
    # The thread's read reaches itself, its four relations and the role's member.
    assert reaches[("scope/part", "via_thread")] == 6


def test_the_report_opens_a_records_named_permissions():
    schema = parse_zed(
        "definition auth/user {}\ndefinition scope/role { relation member: auth/user }\n"
        + PARENT_LINKED.replace(
            "permission edit = owner + resource->edit + team->member",
            "permission own = owner + team->member\n    permission edit = own + resource->edit",
        )
        + MESSAGE_LINKS.replace("    permission share = attached->share\n", "")
    )
    [arrow] = links.narrowed_arrows(schema)
    assert arrow.lent == (
        ("test/nativeparentlinkedchild", "((owner + team->member) + resource->lent(edit))"),
    )


@pytest.mark.django_db
def test_the_command_prints_the_report(settings):
    install_schema(SMALL)
    out = StringIO()
    call_command("rebac", "links", stdout=out)
    text = out.getvalue()
    assert text.startswith("8 arrow(s) narrowed by REBAC_INHERIT_THROUGH_LINKS = False")
    assert "scope/part#via_thread: thread->read (a path of several hops; reaches 6)" in text
    assert "    scope/thread#read lends ((reader + squad->member) + admin->member)" in text
    assert "      5  a reverse relation" in text
    out = StringIO()
    call_command("rebac", "links", "--json", "--type", "scope/part", stdout=out)
    assert json.loads(out.getvalue()) == [
        {
            "lent": {"scope/thread": "((reader + squad->member) + admin->member)"},
            "permission": "via_thread",
            "reaches": 6,
            "reason": "a path of several hops",
            "relation": "thread",
            "resource_type": "scope/part",
            "target": "read",
            "target_types": ["scope/thread"],
        }
    ]
    # The report is about the published policy, whatever the setting.
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    again = StringIO()
    call_command("rebac", "links", "--json", "--type", "scope/part", stdout=again)
    assert again.getvalue() == out.getvalue()


# The relations of the report's random policies, by type: every kind, and a relation to
# two types (``cited``), so that arrows dispatch to several types.
REPORT_RELATIONS = {
    "scope/thread": {
        "reader": "auth/user | auth/user:*",
        "blocked": "auth/user",
        "admin": "scope/role // rebac:const=admin",
        "squad": "scope/role",
        "sibling": "scope/thread",
        "pinned": "scope/message",
        "messages": "scope/message // rebac:field=scopemessage",
    },
    "scope/message": {
        "direct": "auth/user",
        "banned": "auth/user",
        "thread": "scope/thread // rebac:field=thread",
        "profile": "scope/profile // rebac:field=profile",
        "cited": "scope/thread | scope/profile",
        "admin": "scope/role // rebac:const=admin",
    },
    "scope/part": {
        "direct": "auth/user",
        "banned": "auth/user",
        "message": "scope/message // rebac:field=message",
        "thread": "scope/thread // rebac:field=message__thread",
    },
    "scope/profile": {
        "direct": "auth/user",
        "banned": "auth/user",
        "owner": "scope/message // rebac:field=owner",
        "pinned": "scope/thread",
    },
}
REPORT_NAMES = ("p", "q", "read")


def report_tree(rng: random.Random, relations: dict[str, str], earlier: Any, depth: int) -> str:
    """A random tree over the type's own relations, ``earlier`` permissions and arrows."""
    if depth == 0 or rng.random() < 0.3:
        own = [name for name, subjects in relations.items() if subjects.startswith("auth/user")]
        roll = rng.random()
        if roll < 0.3:
            return rng.choice(own)
        if roll < 0.4 and earlier:
            return str(rng.choice(earlier))
        via = rng.choice(sorted(set(relations) - set(own)))
        return f"{via}->{'member' if via in ('admin', 'squad') else rng.choice(REPORT_NAMES)}"
    op = rng.choice(["+", "+", "-", "-", "&"])
    left = report_tree(rng, relations, earlier, depth - 1)
    return f"({left} {op} {report_tree(rng, relations, earlier, depth - 1)})"


def report_policy(seed: int) -> str:
    """Permissions ``p``, ``q`` and ``read`` on each type: random trees of ``+``, ``-`` and
    ``&`` over the type's own relations, its earlier permissions and arrows through every
    relation that names another type, to any of the three names."""
    rng = random.Random(seed)
    text = ["definition auth/user {}", "definition scope/role { relation member: auth/user }"]
    for type_, relations in REPORT_RELATIONS.items():
        lines = [f"    relation {name}: {subjects}" for name, subjects in relations.items()]
        for index, name in enumerate(REPORT_NAMES):
            tree = report_tree(rng, relations, REPORT_NAMES[:index], rng.randint(1, 3))
            lines.append(f"    permission {name} = {tree}")
        text.append(f"definition {type_} {{\n" + "\n".join(lines) + "\n}")
    return "\n".join(text) + "\n"


def arrows_of(expression: Any) -> Iterator[PermArrow]:
    pending = [expression]
    while pending:
        node = pending.pop()
        if isinstance(node, PermBinOp):
            pending += [node.left, node.right]
        elif isinstance(node, PermArrow):
            yield node


@pytest.mark.parametrize("chunk", range(6))
def test_the_report_lists_exactly_what_the_narrowed_policy_lends_through_an_arrow(chunk):
    """The report against the narrowed policy, on random policies whose arrows dispatch to
    one type or to several: an arrow ``via->target`` written in ``T#P`` is reported if and
    only if the narrowed ``T#P`` or ``T#excluded(P)`` holds ``via->lent(target)``."""
    for seed in range(chunk * 50, chunk * 50 + 50):
        schema = parse_zed(report_policy(seed))
        narrowed = links.narrowed(schema)
        held = set()
        for definition in schema.definitions:
            for permission in definition.permissions:
                for name in (permission.name, f"excluded({permission.name})"):
                    found = narrowed.get_permission(definition.resource_type, name)
                    for arrow in arrows_of(found.expression) if found is not None else ():
                        parts = links.generated_parts(arrow.target)
                        if parts is not None and parts[:2] == (True, False):
                            held.add(
                                (definition.resource_type, permission.name, arrow.via, parts[2])
                            )
        reported = {
            (arrow.resource_type, arrow.permission, arrow.relation, arrow.target)
            for arrow in links.narrowed_arrows(schema)
        }
        assert reported == held, (seed, sorted(reported ^ held))


@pytest.mark.django_db
def test_explain_prints_the_published_expression_and_what_is_evaluated(settings):
    install_schema(SMALL)
    out = StringIO()
    call_command("rebac", "explain", "scope/part.via_thread", stdout=out)
    assert out.getvalue() == "scope/part#via_thread = thread->read\n"
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    out = StringIO()
    call_command("rebac", "explain", "scope/part.via_thread", stdout=out)
    assert out.getvalue() == (
        "scope/part#via_thread = thread->read\n"
        "  evaluated without inheritance through links: thread->lent(read)\n"
    )


@pytest.mark.django_db
def test_the_export_writes_the_published_policy(settings, tmp_path):
    from rebac import skeleton

    install_schema(SMALL)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    skeleton.export(tmp_path)
    assert "lent(" not in (tmp_path / "policy.zed").read_text()


# ---------- The system checks ----------


@pytest.mark.django_db
def test_a_warning_says_the_evaluated_policy_is_not_the_published_one(settings):
    from rebac.checks import check_inherit_through_links

    install_schema(SMALL)
    assert check_inherit_through_links() == []
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    [warning] = check_inherit_through_links()
    assert warning.id == "rebac.W012"
