"""Regression probes from the independent inherited-witness review."""

from contextlib import nullcontext

import pytest
from django.db import connection, transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery
from django.test.utils import CaptureQueriesContext

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope
from rebac.testing import install_schema
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

ADMIN = SubjectRef.of("auth/user", "admin")
MEMBER = SubjectRef.of("auth/user", "member")
pytestmark = pytest.mark.pg_delta


def schema(message_read, *, extra_thread="", extra_message="", part_read="message->read", head=""):
    return (
        head
        + """
definition auth/user {}
definition scope/role { relation member: auth/user }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation direct: auth/user
    """
        + extra_thread
        + """
    permission read = admin->member + direct
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    """
        + extra_message
        + """
    permission read = """
        + message_read
        + """
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = """
        + part_read
        + """
}
"""
    )


def rows():
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


def ids(qs):
    return sorted(qs.values_list("pk", flat=True))


# ---------------------------------------------------------------- semantics


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_dangling_target_behind_compile_time_true_intermediate(storage, settings):
    """message.read = authenticated + thread->read: 0.33.0 compiles message_id IS NOT NULL."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(schema("authenticated + thread->read"))
    rows()
    local.write_relationships(
        [RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN)]
    )
    # A part whose message row does not exist (deferred FK inside the test transaction).
    ScopePart._base_manager.bulk_create([ScopePart(id=9, message_id=999, fragment_id=9)])
    outside = ids(ScopePart.objects.with_actor(ADMIN))
    with evaluator_scope():
        inside = ids(ScopePart.objects.with_actor(ADMIN))
        with CaptureQueriesContext(connection) as ctx:
            list(ScopePart.objects.with_actor(ADMIN).values_list("pk"))
        sql = ctx.captured_queries[-1]["sql"]
    ScopePart._base_manager.filter(id=9).delete()
    assert inside == outside == [1, 2, 3, 9]
    assert '"message_id" IS NOT NULL' in sql


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize(
    "unconditional", ["authenticated - blocked", "authenticated & authenticated"]
)
def test_dangling_target_whose_ordinary_lower_bound_is_true(storage, unconditional, settings):
    """Compound TRUE targets must not become live-row witness semi-joins."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    policy = schema(
        "thread->read",
        extra_thread="relation blocked: auth/group",
        head="definition auth/group { relation member: auth/user }\n",
    ).replace(
        "permission read = admin->member + direct",
        f"permission read = (admin->member + direct) + ({unconditional})",
    )
    local = install_schema(policy)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=1, thread_id=1), ScopeMessage(id=8, thread_id=777)]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 8)]
    )
    local.write_relationships(
        [RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN)]
    )
    try:
        for model in (ScopeMessage, ScopePart):
            assert ids(model.objects.with_actor(ADMIN)) == [1, 8]
            with evaluator_scope(), CaptureQueriesContext(connection) as captured:
                assert ids(model.objects.with_actor(ADMIN)) == [1, 8]
            assert '"thread_id" IS NOT NULL' in captured.captured_queries[-1]["sql"]
    finally:
        ScopePart._base_manager.filter(id=8).delete()
        ScopeMessage._base_manager.filter(id=8).delete()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_prepared_scope_after_revocation_keeps_other_grants(storage, settings):
    """Admin also holds message 2 directly; revoke admin after preparing the scope."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(schema("(thread->read + direct)"))
    rows()
    admin_edge = RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN)
    local.write_relationships(
        [admin_edge, RelationshipTuple(ObjectRef("scope/message", "2"), "direct", ADMIN)]
    )
    with evaluator_scope():
        prepared = ScopePart.objects.with_actor(ADMIN).scoped()
        before = ids(prepared)
        local.delete_relationship(admin_edge)
        after_prepared = ids(prepared)
        after_fresh = ids(ScopePart.objects.with_actor(ADMIN).scoped())
    outside = ids(ScopePart.objects.with_actor(ADMIN))
    assert before == [1, 2, 3]
    assert after_prepared == after_fresh == outside == [2]


# ---------------------------------------------------------------- embeddings


EMBED_SCHEMAS = {
    "own_message_admin": schema(
        "admin->member + direct + thread->read",
        extra_message="relation admin: scope/role // rebac:const=admin",
    ),
    "chain_no_own": schema("thread->read + direct"),
    "part_direct_plus": schema(
        "thread->read + direct",
        part_read="message->read + owner",
    ).replace(
        "relation message: scope/message // rebac:field=message",
        "relation message: scope/message // rebac:field=message\n    relation owner: auth/user",
    ),
}


def embeddings(local, actor):
    scoped = lambda: ScopePart.objects.with_actor(actor).scoped()  # noqa: E731
    lazy = lambda: ScopePart.objects.with_actor(actor)  # noqa: E731
    out = {}
    out["root"] = ids(scoped())
    out["exists1"] = ids(
        ScopeMessage._base_manager.filter(Exists(scoped().filter(message=OuterRef("pk"))))
    )
    out["exists1_lazy"] = ids(
        ScopeMessage._base_manager.filter(Exists(lazy().filter(message=OuterRef("pk"))))
    )
    out["exists2"] = ids(
        ScopeThread._base_manager.filter(
            Exists(
                ScopeMessage._base_manager.filter(thread=OuterRef("pk")).filter(
                    Exists(scoped().filter(message=OuterRef("pk")))
                )
            )
        )
    )
    out["exists_self2"] = ids(
        ScopePart._base_manager.filter(
            Exists(
                ScopePart._base_manager.filter(pk=OuterRef("pk")).filter(
                    Exists(scoped().filter(pk=OuterRef("pk")))
                )
            )
        )
    )
    out["subquery"] = sorted(
        (m, f)
        for m, f in ScopeMessage._base_manager.annotate(
            first=Subquery(
                scoped().filter(message=OuterRef("pk")).order_by("pk").values("fragment_id")[:1]
            )
        ).values_list("pk", "first")
    )
    out["reverse_in"] = sorted(
        set(ScopeMessage._base_manager.filter(scopepart__in=scoped()).values_list("pk", flat=True))
    )
    out["pk_in_values"] = ids(ScopePart._base_manager.filter(pk__in=scoped().values("pk")))
    out["or_combined"] = ids(
        ScopePart._base_manager.filter(Q(pk__in=scoped().values("pk")) | Q(fragment_id=3))
    )
    out["identity_expr"] = sorted(
        set(
            ScopeMessage._base_manager.filter(
                local.permission_expression(
                    model=ScopePart,
                    subject=actor,
                    action="read",
                    using="default",
                    identity=F("scopepart__id"),
                )
            ).values_list("pk", flat=True)
        )
    )
    out["held"] = sorted(
        local.held(
            subject=actor, action="read", resource_type="scope/part", ids=["1", "2", "3", "77"]
        )
    )
    out["union"] = sorted(
        scoped()
        .values_list("pk", flat=True)
        .union(ScopePart._base_manager.filter(pk=3).values_list("pk", flat=True))
    )
    return out


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("shape", sorted(EMBED_SCHEMAS))
@pytest.mark.parametrize("actor", [ADMIN, MEMBER], ids=["admin", "member"])
def test_embeddings_inside_and_outside_agree(storage, shape, actor, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(EMBED_SCHEMAS[shape])
    rows()
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN),
            RelationshipTuple(ObjectRef("scope/message", "2"), "direct", MEMBER),
            RelationshipTuple(ObjectRef("scope/thread", "2"), "direct", MEMBER),
        ]
    )
    outside = embeddings(local, actor)
    with evaluator_scope():
        inside = embeddings(local, actor)
    assert inside == outside


# ---------------------------------------------------------------- statement counts


def counts(make, reads=3):
    out = []
    for _ in range(reads):
        with CaptureQueriesContext(connection) as ctx:
            list(make().values_list("pk", flat=True))
        out.append(len(ctx.captured_queries))
    return out


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("case", ["plain", "caveated_target", "expiring", "atomic"])
@pytest.mark.parametrize("actor", [ADMIN, MEMBER], ids=["admin", "member"])
def test_statement_counts(storage, case, actor, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    head = ""
    extra_thread = ""
    if case == "caveated_target":
        head = "caveat enabled(ok bool) { ok }\n"
        extra_thread = "relation flagged: auth/user with enabled"
    if case == "expiring":
        head = "use expiration\n"
        extra_thread = "relation temp: auth/user with expiration"
    text = schema("thread->read + direct", extra_thread=extra_thread, head=head)
    if case == "caveated_target":
        text = text.replace(
            "permission read = admin->member + direct\n}\ndefinition scope/message",
            "permission read = admin->member + direct + flagged\n}\ndefinition scope/message",
        )
    local = install_schema(text)
    rows()
    tuples = [
        RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN),
        RelationshipTuple(ObjectRef("scope/message", "2"), "direct", MEMBER),
    ]
    if case == "caveated_target":
        tuples.append(
            RelationshipTuple(
                ObjectRef("scope/thread", "2"),
                "flagged",
                MEMBER,
                caveat_name="enabled",
                caveat_context={"ok": True},
            )
        )
    local.write_relationships(tuples)
    # Warm the policy and statement caches outside any evaluator.
    list(ScopePart.objects.with_actor(actor).values_list("pk"))
    expected = ids(ScopePart.objects.with_actor(actor))
    context = transaction.atomic() if case == "atomic" else nullcontext()
    with context, evaluator_scope():
        part = counts(lambda: ScopePart.objects.with_actor(actor))
        result = ids(ScopePart.objects.with_actor(actor))
    assert result == expected
    base = 2 if case == "caveated_target" else 1
    assert part == [base + 1, base, base]


# ---------------------------------------------------------------- SQL growth


DIAMOND = """
definition auth/user {}
definition scope/role { relation member: auth/user }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation a: auth/user
    relation b: auth/user
    relation c: auth/user
    permission ta = admin->member + a
    permission tb = admin->member + b
    permission tc = admin->member + c
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation d: auth/user
    permission ma = thread->ta + thread->tb + thread->tc + d
    permission mb = thread->ta + thread->tb + thread->tc
    permission mc = thread->ta + thread->tb + thread->tc
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    permission read = message->ma + message->mb + message->mc
}
"""


@pytest.mark.django_db(transaction=True)
def test_diamond_sql_size():
    local = install_schema(DIAMOND)
    rows()
    local.write_relationships(
        [RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN)]
    )
    expected = ids(ScopePart.objects.with_actor(ADMIN))
    with evaluator_scope():
        got = ids(ScopePart.objects.with_actor(ADMIN))
    assert got == expected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("raw_writer", [True, False])
def test_inherited_revocation_is_denial_only_until_next_scope(storage, raw_writer, settings):
    """A revoked inherited witness may narrow exactly like an own witness."""
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(schema("thread->read"))
    rows()
    admin_edge = RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN)
    local.write_relationships(
        [admin_edge, RelationshipTuple(ObjectRef("scope/thread", "1"), "direct", ADMIN)]
    )
    with evaluator_scope():
        prepared = ScopePart.objects.with_actor(ADMIN).scoped()
        own = ScopeThread.objects.with_actor(ADMIN).scoped()
        assert ids(prepared) == [1, 2, 3]
        assert ids(own) == [1, 2]
        if raw_writer:
            # The same out-of-band write as the 0.33.0 own-arm contract.
            from rebac.models import active_relationship_model

            active_relationship_model()._base_manager.filter(relation="member").delete()
        else:
            local.delete_relationship(admin_edge)
        after_prepared = ids(prepared)
        after_fresh = ids(ScopePart.objects.with_actor(ADMIN).scoped())
        assert after_prepared == []
        if raw_writer:
            assert (
                after_prepared
                == after_fresh
                == ids(own)
                == ids(ScopeThread.objects.with_actor(ADMIN).scoped())
                == []
            )
    # Every scope starts with a new witness, so the retained direct grant wins.
    with evaluator_scope():
        exact = ids(ScopePart.objects.with_actor(ADMIN))
        assert exact == [1, 2]
        assert ids(ScopeThread.objects.with_actor(ADMIN)) == [1]
    assert set(after_prepared) <= set(exact)
    assert set(after_fresh) <= set(exact)


GFK = """
definition auth/user {}
definition scope/role { relation member: auth/user }
definition blog/folder {
    relation admin: scope/role // rebac:const=admin
    relation viewer: auth/user
    permission read = admin->member + viewer
}
definition blog/post {
    relation owner: auth/user
    permission read = owner
}
definition test/attachment {
    relation folder: blog/folder // rebac:field=target
    relation post:   blog/post   // rebac:field=target
    permission read = folder->read + post->read
}
"""


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("actor", [ADMIN, MEMBER], ids=["admin", "member"])
def test_generic_fk_arrow(storage, actor, settings):
    from django.contrib.contenttypes.models import ContentType

    from tests.testapp.models import Attachment, Folder, Post

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(GFK)
    folder, other = Folder._base_manager.bulk_create([Folder(name="f"), Folder(name="g")])
    [post] = Post._base_manager.bulk_create([Post(title="p")])
    fct = ContentType.objects.get_for_model(Folder)
    pct = ContentType.objects.get_for_model(Post)
    Attachment._base_manager.bulk_create(
        [
            Attachment(content_type=fct, object_id=folder.pk, label="folder"),
            Attachment(content_type=fct, object_id=other.pk, label="other"),
            Attachment(content_type=pct, object_id=post.pk, label="post"),
            Attachment(content_type=fct, object_id=987654, label="dangling-folder"),
            Attachment(content_type=pct, object_id=folder.pk, label="post-ct-with-folder-id"),
        ]
    )
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN),
            RelationshipTuple(ObjectRef("blog/folder", str(folder.pk)), "viewer", MEMBER),
            RelationshipTuple(ObjectRef("blog/post", str(post.pk)), "owner", MEMBER),
        ]
    )

    def labels(qs):
        return sorted(qs.values_list("label", flat=True))

    outside = labels(Attachment.objects.with_actor(actor))
    with evaluator_scope():
        inside = labels(Attachment.objects.with_actor(actor))
        embedded = labels(
            Attachment._base_manager.filter(
                Exists(Attachment.objects.with_actor(actor).scoped().filter(pk=OuterRef("pk")))
            )
        )
    assert inside == outside == embedded


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("case", ["plain", "caveated_target", "expiring", "atomic"])
@pytest.mark.parametrize("actor", [ADMIN, MEMBER], ids=["admin", "member"])
def test_own_witness_statement_counts(storage, case, actor, settings):
    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    text = schema(
        "(admin->member + direct) + thread->read",
        extra_message="relation admin: scope/role // rebac:const=admin",
    )
    if case == "caveated_target":
        text = "caveat enabled(ok bool) { ok }\n" + text.replace(
            "relation direct: auth/user", "relation direct: auth/user | auth/user with enabled"
        )
    elif case == "expiring":
        text = "use expiration\n" + text.replace(
            "relation member: auth/user", "relation member: auth/user with expiration"
        )
    local = install_schema(text)
    rows()
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("scope/role", "admin"), "member", ADMIN),
            RelationshipTuple(ObjectRef("scope/message", "2"), "direct", MEMBER),
        ]
    )
    ids(ScopeMessage.objects.with_actor(actor))
    with transaction.atomic() if case == "atomic" else nullcontext(), evaluator_scope():
        observed = counts(lambda: ScopeMessage.objects.with_actor(actor))
    base = 2 if case == "caveated_target" else 1
    repeat = base + (actor == ADMIN and case in ("atomic", "expiring"))
    assert observed == [base + 1, repeat, repeat]
