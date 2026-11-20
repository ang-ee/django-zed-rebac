"""Names read one way (``rebac.schema.ast.name_conflicts``).

Inside an expression a name is read as the compiler reads it: a built-in's name is the actor's
state, and ``member`` reads a relation before a permission of the same name. A definition that
declares a built-in's name as a relation, a definition or a subject type, or one name twice, lets a
reader that asks for the other thing read something else: the implication prover (proposal 0022)
equated a relation named ``authenticated`` with the built-in, and structural prefetch delivered
through it. ``validate_schema`` states the rules. New input is held to them at runtime
(``set_schema``, ``install_schema``, a stored-policy write, by what the write adds); a stored policy
that already holds such a place keeps deciding, the database check ``rebac.W014`` reports it, and
every experimental read mechanism reads it by its statements.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.core.checks import WARNING, run_checks
from django.db import connection

from rebac import ObjectRef, RelationshipTuple, SubjectRef, rowscope
from rebac.backends import backend, reset_backend
from rebac.backends.local import LocalBackend
from rebac.compile import read
from rebac.errors import SchemaError
from rebac.models import SchemaDefinition, SchemaPermission, SchemaRelation
from rebac.models.generation import SchemaGeneration
from rebac.models.schema_write import schema_index_write
from rebac.schema.ast import backing_to_dict, name_conflicts, schema_name_conflicts
from rebac.schema.parser import parse_zed, validate_schema
from rebac.schema.rendering import _render_expr
from rebac.testing import install_schema
from tests.testapp.models import ScopeMessage, ScopeThread

MEMBER = SubjectRef.of("auth/user", "member")


def grant(
    type_: str, id_: object, relation: str, subject: SubjectRef = MEMBER
) -> RelationshipTuple:
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)


def store(schema_text: str) -> dict[tuple[str, str], Any]:
    """Store a policy through its write owners; the backend then reads it."""
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


def rename_behind_the_write_owners(model: Any, old: str, new: str) -> None:
    """Rename a stored name with SQL, as a policy stored before the rule was held at runtime
    could hold it, and publish a revision so that the backend reads it."""
    meta = model._meta
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET name = %s WHERE name = %s",
            [new, old],
        )
    SchemaGeneration.objects.filter(pk=1).update(revision="after-upgrade")
    backend().mark_schema_stale()


def w014(databases: list[str] | None) -> list[Any]:
    return [issue for issue in run_checks(databases=databases) if issue.id == "rebac.W014"]


# ---------- The rule ----------

# A thread's relation and permission both named ``x``: the compiler reads the relation.
SAME_NAME = """
definition auth/user {}
definition scope/thread {
    relation x: auth/user
    relation everyone: auth/user
    permission x = everyone
    permission read = x
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread
    relation direct: auth/user
    permission read = direct + linked->x
}
"""
TWO_WAYS = {
    "a relation and a permission of one name": (
        SAME_NAME,
        "scope/thread#x: name collision between a relation and a permission",
    ),
    "a relation named as a built-in": (
        SAME_NAME.replace("relation x: auth/user", "relation authenticated: auth/user"),
        "scope/thread#authenticated: reserved built-in actors cannot be declared as relations",
    ),
    "two relations of one name": (
        SAME_NAME.replace(
            "relation everyone: auth/user",
            "relation everyone: auth/user\n    relation everyone: auth/user",
        ),
        "scope/thread#everyone: duplicate relation name",
    ),
    "two permissions of one name": (
        SAME_NAME.replace("permission read = x", "permission read = x\n    permission read = x"),
        "scope/thread#read: duplicate permission name",
    ),
    "a built-in as a subject type": (
        SAME_NAME.replace("relation direct: auth/user", "relation direct: anonymous"),
        "scope/message#direct: reserved built-in actor 'anonymous' is valid only inside "
        "permission expressions",
    ),
    "a built-in as a definition": (
        SAME_NAME + "definition anonymous {}\n",
        "'anonymous' is a reserved built-in actor and cannot be declared as a definition",
    ),
}


@pytest.mark.parametrize("case", sorted(TWO_WAYS))
def test_new_input_may_not_read_a_name_two_ways(case):
    """``validate_schema`` states each rule once, through ``name_conflicts``, and the backend
    holds new input to it, whoever built the schema."""
    text, message = TWO_WAYS[case]
    if case != "a relation and a permission of one name":
        text = text.replace("permission x = everyone", "permission xperm = everyone")
    schema = parse_zed(text)
    assert [error for error in validate_schema(schema) if message in error] == [message]
    assert message in schema_name_conflicts(schema)
    with pytest.raises(SchemaError, match=message.split(":")[0]):
        LocalBackend().set_schema(schema)
    with pytest.raises(SchemaError, match=message.split(":")[0]):
        install_schema(text)


def test_a_place_counts_once_per_occurrence():
    schema = parse_zed(
        SAME_NAME.replace(
            "relation everyone: auth/user",
            "relation everyone: auth/user\n    relation everyone: auth/user\n"
            "    relation everyone: auth/user",
        )
    )
    thread = schema.get_definition("scope/thread")
    assert thread is not None
    assert name_conflicts(thread) == [
        "scope/thread#everyone: duplicate relation name",
        "scope/thread#everyone: duplicate relation name",
        "scope/thread#x: name collision between a relation and a permission",
    ]


@pytest.mark.django_db
def test_a_schema_that_reads_each_name_one_way_has_no_conflict():
    schema = parse_zed(SAME_NAME.replace("permission x = everyone", "permission xperm = everyone"))
    assert schema_name_conflicts(schema) == []
    assert validate_schema(schema) == []
    install_schema(SAME_NAME.replace("permission x = everyone", "permission xperm = everyone"))


# ---------- Stored policies ----------


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_stored_write_may_not_add_a_name_read_two_ways():
    with pytest.raises(SchemaError, match=r"scope/thread#x: name collision.*\(rebac\.W014\)"):
        store(SAME_NAME)
    assert not SchemaDefinition.objects.exists()
    store(SAME_NAME.replace("permission x = ", "permission xperm = "))
    message = SchemaDefinition.objects.get(resource_type="scope/message")
    with pytest.raises(SchemaError, match=r"scope/message#linked: name collision.*rebac\.W014"):
        SchemaPermission.objects.create(definition=message, name="linked", expression="direct")
    with pytest.raises(SchemaError, match=r"reserved built-in actors.*rebac\.W014"):
        SchemaRelation.objects.create(
            definition=message, name="anonymous", allowed_subjects=[{"type": "auth/user"}]
        )
    assert not SchemaPermission.objects.filter(name="linked").exists()
    assert not SchemaRelation.objects.filter(name="anonymous").exists()


@pytest.fixture
def two_ways_stored(settings):
    """``SAME_NAME`` as a stored policy from before the rule was held at runtime: stored with the
    permission named ``xperm``, then renamed ``x`` outside the write owners. The member is in
    thread 1's ``everyone``; message 1 is linked to thread 1."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    store(SAME_NAME.replace("permission x = ", "permission xperm = "))
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1), ScopeThread(id=2)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=1, thread_id=1), ScopeMessage(id=2, thread_id=2)]
    )
    backend().write_relationships(
        [
            grant("scope/thread", 1, "everyone"),
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
        ]
    )
    rename_behind_the_write_owners(SchemaPermission, "xperm", "x")


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_stored_policy_that_reads_a_name_two_ways_keeps_deciding(two_ways_stored):
    """It decides as it did: ``x`` is the relation, which nobody holds. The database check warns,
    a write beside it proceeds, and a repair clears the warning."""
    readable = set(ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True))
    assert readable == set()
    assert read._policy(backend()).name_conflicts == (
        "scope/thread#x: name collision between a relation and a permission",
    )
    [issue] = w014(["default"])
    assert issue.level == WARNING and "scope/thread#x: name collision" in issue.msg
    assert w014(None) == []
    # A write beside it adds no place: it proceeds, and so does a schema write that adds none.
    backend().write_relationships([grant("scope/message", 2, "direct")])
    message = SchemaDefinition.objects.get(resource_type="scope/message")
    SchemaPermission.objects.create(definition=message, name="browse", expression="direct")
    assert set(ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True)) == {2}
    # Another place is refused beside it.
    with pytest.raises(SchemaError, match=r"scope/message#direct: name collision.*rebac\.W014"):
        SchemaPermission.objects.create(definition=message, name="direct", expression="direct")
    SchemaPermission.objects.filter(definition__resource_type="scope/thread", name="x").update(
        name="xperm"
    )
    assert w014(["default"]) == []
    assert read._policy(backend()).name_conflicts == ()


# The thread's relation named ``authenticated`` beside ``read = authenticated``, which inside the
# expression is the actor's state: every authenticated actor reads a thread, and no actor is in
# the relation. A message's ``read`` names the relation through ``thread``; its ``browse`` names
# the permission.
BUILT_IN_RELATION = """
definition auth/user {}
definition scope/thread {
    relation authenticated: auth/user
    permission read = authenticated
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    permission read = thread->authenticated
    permission browse = thread->read
}
"""


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_every_experimental_mechanism_reads_it_by_its_statements(settings, storage):
    """Stored, then the relation renamed ``authenticated`` outside the write owners. The prover
    proved ``read => authenticated`` on the thread, so structural prefetch delivered message 1
    under thread 1, where the relation it reads is empty. Structural prefetch, the bounded walk
    and the row scope now read the policy by its statements."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    store(BUILT_IN_RELATION.replace("relation authenticated:", "relation authx:"))
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=1, thread_id=1)])
    rename_behind_the_write_owners(SchemaRelation, "authx", "authenticated")
    assert read._policy(backend()).name_conflicts

    def threads_messages(structural_on: bool) -> dict[int, list[int]]:
        settings.REBAC_STRUCTURAL_PREFETCH = structural_on
        rows = (
            ScopeThread.objects.with_actor(MEMBER)
            .order_by("pk")
            .rebac_prefetch_related("scopemessage_set")
        )
        return {t.pk: sorted(m.pk for m in t.scopemessage_set.all()) for t in rows}

    assert threads_messages(True) == threads_messages(False) == {1: []}
    settings.REBAC_ROW_SCOPE = True
    told = rowscope.explain(ScopeMessage, MEMBER, "read")
    assert not told["applies"] and "reads two ways" in told["blocked"][0]
    assert list(ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True)) == []
    for mode in ("prove", "decide"):
        settings.REBAC_BOUNDED_WALK = mode
        held = backend().held(
            subject=MEMBER, action="read", resource_type="scope/message", ids=["1"]
        )
        browsed = backend().held(
            subject=MEMBER, action="browse", resource_type="scope/message", ids=["1"]
        )
        assert (held, browsed) == (set(), {"1"}), mode


# ---------- What stops a stored policy from loading is chosen by the rule ----------


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("name", ["x", "backing", "feedbacking"])
def test_a_name_read_two_ways_loads_whatever_it_contains(settings, name):
    """A relation and a permission of one name, on a modelled type and on a type named
    ``doc/backing_store``, stored before the rule was held at runtime. The load errors were chosen
    by the text ``"backing"`` in ``validate_schema``'s findings: these names made the policy
    unloadable, every protected read and the database checks raised. It loads, decides as the
    published names read (the relation, which nobody holds), and W014 reports each place."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = "denormalized"
    text = SAME_NAME.replace("x", name).replace("relation everyone", "relation everyone") + (
        "definition doc/backing_store {\n    relation y: auth/user\n    permission yperm = y\n}\n"
    )
    store(text.replace(f"permission {name} = ", f"permission {name}perm = "))
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create([ScopeMessage(id=1, thread_id=1)])
    backend().write_relationships(
        [
            grant("scope/thread", 1, "everyone"),
            grant("scope/message", 1, "linked", SubjectRef.of("scope/thread", "1")),
        ]
    )
    rename_behind_the_write_owners(SchemaPermission, f"{name}perm", name)
    rename_behind_the_write_owners(SchemaPermission, "yperm", "y")
    assert list(ScopeMessage.objects.with_actor(MEMBER).values_list("pk", flat=True)) == []
    assert list(ScopeThread.objects.with_actor(MEMBER).values_list("pk", flat=True)) == []
    reported = sorted(issue.msg for issue in w014(["default"]))
    assert len(reported) == 2
    assert f"scope/thread#{name}: name collision" in reported[1]
    assert "doc/backing_store#y: name collision" in reported[0]


@pytest.mark.django_db
def test_a_type_named_backing_with_an_unknown_reference_is_installed():
    """An unknown reference grants nothing, and does not stop a policy from loading. In a type
    whose name contains ``backing`` it did: its finding names the type."""
    local = install_schema(
        """
definition auth/user {}
definition doc/backing {
    relation viewer: auth/user
    permission read = viewer + missing
}
"""
    )
    assert validate_schema(local.schema()) == [
        "doc/backing: undefined reference 'missing' in expression"
    ]


@pytest.mark.django_db
def test_a_backed_relation_of_the_wrong_shape_is_still_refused():
    """Installed, and stored: the backed relations' rules are the ones the runtime enforces."""
    shaped = SAME_NAME.replace("permission x = everyone", "permission xperm = everyone")
    wrong = shaped.replace(
        "relation thread: scope/thread // rebac:field=thread",
        "relation thread: scope/thread | auth/user // rebac:field=thread",
    )
    with pytest.raises(SchemaError, match="backed relation must declare exactly one subject type"):
        install_schema(wrong)
    store(shaped)
    meta = SchemaRelation._meta
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET allowed_subjects = %s "
            "WHERE name = %s",
            ['[{"type": "scope/thread"}, {"type": "auth/user"}]', "thread"],
        )
    SchemaGeneration.objects.filter(pk=1).update(revision="after-upgrade")
    backend().mark_schema_stale()
    with pytest.raises(SchemaError, match="backed relation must declare exactly one subject type"):
        list(ScopeMessage.objects.with_actor(MEMBER))


# ---------- Repairs ----------

COMPOSED = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    relation editor: auth/user
    permission a = reader
    permission b = a
    permission read = a
}
"""


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_an_override_that_fails_to_compose_can_be_deleted():
    """An expired extend override ``a += b`` where ``b = a``: the decisions compose only the
    overrides in force, the write owner composes every stored row and finds a cycle. That is a
    policy being repaired, as an unreadable one is: deleting the override is allowed."""
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    from rebac.models import SchemaOverride

    found = store(COMPOSED)
    row = SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_EXTEND,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=found[("scope/thread", "a")].pk,
        expression="editor",
        reason="test",
    )
    meta = SchemaOverride._meta
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET expression = %s, "
            f"expires_at = %s WHERE {connection.ops.quote_name(meta.pk.column)} = %s",
            ["b", timezone.now() - timezone.timedelta(days=1), row.pk],
        )
    backend().mark_schema_stale()
    SchemaOverride.objects.filter(pk=row.pk).delete()
    assert not SchemaOverride.objects.exists()
    definition = SchemaDefinition.objects.get(resource_type="scope/thread")
    SchemaPermission.objects.create(definition=definition, name="c", expression="reader")


@pytest.mark.django_db
@pytest.mark.pg_delta
def test_a_repair_may_remove_a_name_read_two_ways_never_add_one():
    """A stored policy that cannot be read (a relation's backing that does not decode): a write
    repairs it without being validated again, but it may not add a place where a name is read
    two ways; it may remove one."""
    store(SAME_NAME.replace("permission x = ", "permission xperm = "))
    rename_behind_the_write_owners(SchemaPermission, "xperm", "x")
    meta = SchemaRelation._meta
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET backing = %s WHERE name = %s",
            ['{"kind": "fk", "path": "thread", "bogus": 1}', "thread"],
        )
    backend().mark_schema_stale()
    message = SchemaDefinition.objects.get(resource_type="scope/message")
    with pytest.raises(SchemaError, match=r"reserved built-in actors.*rebac\.W014"):
        SchemaRelation.objects.create(
            definition=message, name="authenticated", allowed_subjects=[{"type": "auth/user"}]
        )
    assert not SchemaRelation.objects.filter(name="authenticated").exists()
    # A built-in as a subject type of an existing relation is a place too.
    direct = SchemaRelation.objects.get(definition=message, name="direct")
    direct.allowed_subjects = [{"type": "auth/user"}, {"type": "anonymous"}]
    with pytest.raises(SchemaError, match=r"reserved built-in actor 'anonymous'.*rebac\.W014"):
        direct.save()
    direct.refresh_from_db()
    assert [item["type"] for item in direct.allowed_subjects] == ["auth/user"]
    # Repairs: the collision renamed away, then the backing fixed.
    SchemaPermission.objects.filter(definition__resource_type="scope/thread", name="x").update(
        name="xperm"
    )
    relation = SchemaRelation.objects.get(definition=message, name="thread")
    relation.backing = {"kind": "fk", "path": "thread"}
    relation.save()
    assert read._policy(backend()).name_conflicts == ()


@pytest.mark.django_db
@pytest.mark.pg_delta
@pytest.mark.parametrize("expired", [True, False], ids=["expired", "in force"])
def test_an_override_that_does_not_parse_can_be_deleted(expired):
    """An override expression written past the guards that does not parse. Expired, decisions do
    not read it, and the write owner, which reads every stored override, made every write raise as
    it started; in force, nothing can read the policy. Either way it is a policy being repaired,
    and deleting the override is allowed."""
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    from rebac.models import SchemaOverride

    found = store(COMPOSED)
    row = SchemaOverride.objects.create(
        kind=SchemaOverride.KIND_EXTEND,
        target_ct=ContentType.objects.get_for_model(SchemaPermission),
        target_pk=found[("scope/thread", "a")].pk,
        expression="editor",
        reason="test",
    )
    meta = SchemaOverride._meta
    when = timezone.now() + timezone.timedelta(days=-1 if expired else 1)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {connection.ops.quote_name(meta.db_table)} SET expression = %s, "
            f"expires_at = %s WHERE {connection.ops.quote_name(meta.pk.column)} = %s",
            ["editor +", when, row.pk],
        )
    backend().mark_schema_stale()
    SchemaOverride.objects.filter(pk=row.pk).delete()
    assert not SchemaOverride.objects.exists()
    definition = SchemaDefinition.objects.get(resource_type="scope/thread")
    SchemaPermission.objects.create(definition=definition, name="c", expression="reader")
