"""Randomised differential: scoped reads inside an evaluator equal reads outside one."""

import random
from datetime import timedelta

import pytest
from django.utils import timezone

from rebac import ObjectRef, RelationshipTuple, SubjectRef, evaluator_scope
from rebac.testing import install_schema
from tests.reference_harness import assert_scope_matches
from tests.testapp.models import ScopeMessage, ScopePart, ScopeThread

ADMIN = SubjectRef.of("auth/user", "admin")
MEMBER = SubjectRef.of("auth/user", "member")
GROUPSET = SubjectRef.of("auth/group", "g", "member")


def expr(rng, atoms, depth):
    if depth == 0 or rng.random() < 0.35:
        return rng.choice(atoms)
    op = rng.choice(["+", "+", "+", "+", "&", "-"])
    return f"({expr(rng, atoms, depth - 1)} {op} {expr(rng, atoms, depth - 1)})"


def build(seed):
    rng = random.Random(seed)
    t_atoms = ["admin->member", "direct", "banned", "cav", "grp", "pub"]
    t_view = expr(rng, t_atoms, 2)
    t_read = expr(rng, [*t_atoms, "view"], 2)
    m_atoms = [
        "admin->member",
        "direct",
        "banned",
        "thread->read",
        "thread->view",
        "thread->read",
        "thread->view",
    ]
    m_view = expr(rng, m_atoms, 2)
    m_read = expr(rng, [*m_atoms, "view"], 2)
    p_atoms = [
        "direct",
        "banned",
        "message->read",
        "message->view",
        "message->read",
        "message->view",
    ]
    p_read = expr(rng, p_atoms, 2)
    # Half the seeds must exercise the optimization, not merely its fallback.
    inheriting = seed % 2 == 0
    if inheriting:
        t_read = f"(admin->member + direct) + ({t_read})"
        m_read = f"thread->read + ({m_read})"
        p_read = f"message->read + ({p_read})"
    m_admin = rng.choice(["admin", "madmin"])
    schema = f"""
use expiration
caveat enabled(ok bool) {{ ok }}
definition auth/user {{}}
definition auth/group {{ relation member: auth/user }}
definition scope/role {{ relation member: auth/user | auth/group#member | auth/user with expiration }}
definition scope/thread {{
    relation admin: scope/role // rebac:const=admin
    relation direct: auth/user
    relation banned: auth/user | auth/user with enabled
    relation cav: auth/user with enabled
    relation grp: auth/group#member
    relation pub: auth/user:*
    permission view = {t_view}
    permission read = {t_read}
}}
definition scope/message {{
    relation admin: scope/role // rebac:const={m_admin}
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    relation banned: auth/user
    permission view = {m_view}
    permission read = {m_read}
}}
definition scope/part {{
    relation message: scope/message // rebac:field=message
    relation direct: auth/user
    relation banned: auth/user
    permission read = {p_read}
}}
"""
    tuples = []
    now = timezone.now()
    for role in ("admin", "madmin"):
        roll = rng.random()
        if roll < 0.6 or (inheriting and role == "admin"):
            tuples.append(RelationshipTuple(ObjectRef("scope/role", role), "member", ADMIN))
        elif roll < 0.7:
            tuples.append(
                RelationshipTuple(
                    ObjectRef("scope/role", role),
                    "member",
                    ADMIN,
                    expires_at=now - timedelta(seconds=5),
                )
            )
        elif roll < 0.8:
            tuples.append(RelationshipTuple(ObjectRef("scope/role", role), "member", GROUPSET))
    if rng.random() < 0.6:
        tuples.append(
            RelationshipTuple(ObjectRef("auth/group", "g"), "member", rng.choice([ADMIN, MEMBER]))
        )
    for type_, n in (("scope/thread", 2), ("scope/message", 3), ("scope/part", 3)):
        for oid in range(1, n + 1):
            for rel in ("direct", "banned"):
                if rng.random() < 0.3:
                    tuples.append(
                        RelationshipTuple(
                            ObjectRef(type_, str(oid)), rel, rng.choice([ADMIN, MEMBER])
                        )
                    )
    for oid in (1, 2):
        if rng.random() < 0.3:
            tuples.append(
                RelationshipTuple(
                    ObjectRef("scope/thread", str(oid)),
                    "cav",
                    rng.choice([ADMIN, MEMBER]),
                    caveat_name="enabled",
                    caveat_context=rng.choice([{"ok": True}, {"ok": False}, None]),
                )
            )
        if rng.random() < 0.2:
            tuples.append(
                RelationshipTuple(
                    ObjectRef("scope/thread", str(oid)),
                    "banned",
                    ADMIN,
                    caveat_name="enabled",
                    caveat_context=rng.choice([{"ok": True}, None]),
                )
            )
        if rng.random() < 0.2:
            tuples.append(RelationshipTuple(ObjectRef("scope/thread", str(oid)), "grp", GROUPSET))
        if rng.random() < 0.2:
            tuples.append(
                RelationshipTuple(
                    ObjectRef("scope/thread", str(oid)), "pub", SubjectRef.of("auth/user", "*")
                )
            )
    return schema, tuples


def ids(qs):
    return sorted(qs.values_list("pk", flat=True))


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.pg_delta
@pytest.mark.parametrize("seed", range(24))
def test_randomized_inherited_scope_matches_reference(seed, storage, settings, monkeypatch):
    _compare(seed, storage, settings, monkeypatch, dangling=False)


@pytest.mark.django_db
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.pg_delta
@pytest.mark.parametrize("seed", range(24))
def test_randomized_inherited_scope_with_dangling_rows(seed, storage, settings, monkeypatch):
    try:
        _compare(seed, storage, settings, monkeypatch, dangling=True)
    finally:
        ScopePart._base_manager.filter(id__in=(8, 9)).delete()
        ScopeMessage._base_manager.filter(id=8).delete()


def _compare(seed, storage, settings, monkeypatch, *, dangling):
    from rebac.compile.predicate import Bound, Compiler

    settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    schema, tuples = build(seed)
    local = install_schema(schema)
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
    if dangling:
        ScopeMessage._base_manager.bulk_create([ScopeMessage(id=8, thread_id=777)])
        ScopePart._base_manager.bulk_create(
            [
                ScopePart(id=8, message_id=8, fragment_id=8),
                ScopePart(id=9, message_id=999, fragment_id=9),
            ]
        )
    if tuples:
        local.write_relationships(tuples)
    witnessed = []
    original = Compiler._holds

    def observed(self, key, *args, **kwargs):
        if key in self.witnessed and args[1] is Bound.LOWER:
            witnessed.append(key)
        return original(self, key, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Compiler, "_holds", observed)
        for actor in (ADMIN, MEMBER, GROUPSET):
            for model in (ScopePart, ScopeMessage):
                outside = ids(model.objects.with_actor(actor))
                with evaluator_scope():
                    inside = ids(model.objects.with_actor(actor))
                    again = ids(model.objects.with_actor(actor).scoped())
                    if not dangling:
                        assert_scope_matches(model, actor=actor, action="read")
                    # The reference walker traverses FK tuples to absent model
                    # rows. For dangling rows, the ordinary compiler is the
                    # compatibility oracle; valid-row seeds also use the walker.
                assert inside == outside == again, (actor, model.__name__, schema)
    if seed % 2 == 0:
        assert witnessed, (seed, storage, schema)
