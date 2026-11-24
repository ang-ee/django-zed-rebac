"""A name in an expression means what it means to the compiler, in every mechanism (proposal 0020).

``anonymous`` and ``authenticated`` inside a permission expression are the actor's state,
whatever the definition declares under those names. The union arms structural prefetch and
the bounded walk read are collected the same way: a permission declared as ``anonymous`` is
not one of ``read``'s arms below, and an authenticated actor holds ``read`` only directly.
"""

from __future__ import annotations

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef
from rebac.testing import install_schema
from tests.testapp.models import ScopeMessage, ScopeThread

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")
SCHEMA = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission anonymous = thread->read
    permission read = anonymous + direct
}
"""


@pytest.fixture
def seeded():
    local = install_schema(SCHEMA)
    ScopeThread._base_manager.bulk_create([ScopeThread(id=1)])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=1, thread_id=1), ScopeMessage(id=2, thread_id=1)]
    )
    local.write_relationships(
        [
            RelationshipTuple(ObjectRef("scope/thread", "1"), "reader", MEMBER),
            RelationshipTuple(ObjectRef("scope/message", "2"), "direct", MEMBER),
        ]
    )
    return local


def test_structural_prefetch_reads_a_built_in_name_as_the_compiler_does(seeded, settings):
    def threads():
        rows = ScopeThread.objects.with_actor(MEMBER).rebac_prefetch_related("scopemessage_set")
        return {t.pk: sorted(m.pk for m in t.scopemessage_set.all()) for t in rows}

    settings.REBAC_STRUCTURAL_PREFETCH = False
    assert threads() == {1: [2]}
    settings.REBAC_STRUCTURAL_PREFETCH = True
    assert threads() == {1: [2]}


@pytest.mark.parametrize("walk", ["prove", "decide"])
def test_the_bounded_walk_reads_a_built_in_name_as_the_compiler_does(seeded, settings, walk):
    def held() -> set[str]:
        return set(
            seeded.held(
                subject=MEMBER, action="read", resource_type="scope/message", ids=["1", "2"]
            )
        )

    settings.REBAC_BOUNDED_WALK = False
    assert held() == {"2"}
    settings.REBAC_BOUNDED_WALK = walk
    assert held() == {"2"}
