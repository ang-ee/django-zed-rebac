"""Display rows (proposal 0024) under a policy without inheritance through links (proposal 0023).

The two experiments meet where a ``for_display()`` prefetch decides. Its parents
are the rows a queryset scoped for the actor admitted under their model's read
action, which ``REBAC_INHERIT_THROUGH_LINKS = False`` evaluates narrowed; the
field gates it consults are declared names, the same in both policies. A name
the narrowing generates is not an action anywhere, so rows scoped under one
admit nothing. The display read issues no permission statement and runs outside
every window in which the published policy answers a generated name, on every
path a queryset is evaluated by. A scope that raised (a narrowing that failed or
was refused) is built again by the next evaluation, so it never reads, or
admits, rows it did not scope. The checks of both features run once each, under
their own ids.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection
from django.db.models import Prefetch
from django.db.models.expressions import ColPairs
from django.db.models.fields.tuple_lookups import TupleIn
from django.test.utils import CaptureQueriesContext

from rebac import SubjectRef, display, links, rowscope, structural
from rebac.backends import local as local_backend
from rebac.compile import read as compiled
from rebac.conf import app_settings
from rebac.errors import SchemaError
from rebac.schema.walker import field_gated_actions
from rebac.testing import install_schema
from tests.mechanism_policies import grant
from tests.testapp.models import (
    LabAccount,
    LabNote,
    ScopeMessage,
    ScopePart,
    ScopeProfile,
    ScopeThread,
)

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
MEMBER = SubjectRef.of("auth/user", "member")

# ``thread`` is a message's own foreign key (a parent: kept whole); ``linked`` and ``keeper`` are
# stored relations to records of another type (links). A thread lends its own readers through
# ``linked``, not what it holds through its keeper.
SCHEMA = """
definition auth/user {}
definition scope/profile {
    relation reader: auth/user
    permission read = reader
}
definition scope/thread {
    relation reader: auth/user
    relation keeper: scope/profile
    permission read = reader + keeper->read
    permission write = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation linked: scope/thread
    relation direct: auth/user
    permission read = direct + linked->read
    permission write = direct
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    relation direct: auth/user
    permission read = direct
}
"""
# A gate on the parent's foreign key whose own expression the narrowing changes.
GATED_KEY = SCHEMA.replace(
    "    permission write = direct\n}\ndefinition scope/part",
    "    permission write = direct\n    permission read__thread = linked->read\n}\n"
    "definition scope/part",
)
# A gate on the one display field.
GATED_LABEL = SCHEMA.replace(
    "    permission write = reader\n}",
    "    permission write = reader\n    permission read__label = keeper->read\n}",
)
assert SCHEMA != GATED_KEY and SCHEMA != GATED_LABEL

# The member reads message 1 directly, message 2 through thread 2's own reader, and message 3
# through thread 3's keeper, which thread 3 does not lend; message 4 not at all.
GRANTS = [
    grant("scope/message", 1, "direct"),
    grant("scope/message", 2, "linked", SubjectRef.of("scope/thread", "2")),
    grant("scope/thread", 2, "reader"),
    grant("scope/message", 3, "linked", SubjectRef.of("scope/thread", "3")),
    grant("scope/thread", 3, "keeper", SubjectRef.of("scope/profile", "1")),
    grant("scope/profile", 1, "reader"),
    *(grant("scope/part", n, "direct") for n in (1, 2, 3, 4)),
]
PUBLISHED = {1, 2, 3}
NARROWED = {1, 2}
LABELS = {1: "first", 2: "second", 3: "third"}


def seed() -> None:
    """Threads 1-3; message ``n`` in thread ``n``, message 4 in thread 3; part ``n`` of message
    ``n``; the profile thread 3 is kept by."""
    ScopeThread._base_manager.bulk_create([ScopeThread(id=n, label=s) for n, s in LABELS.items()])
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=n, thread_id=t) for n, t in ((1, 1), (2, 2), (3, 3), (4, 3))]
    )
    ScopePart._base_manager.bulk_create(
        [ScopePart(id=n, message_id=n, fragment_id=n) for n in (1, 2, 3, 4)]
    )
    ScopeProfile._base_manager.bulk_create([ScopeProfile(id=1, owner_id=1)])


def install(settings: Any, schema: str = SCHEMA, storage: str | None = None) -> Any:
    if storage is not None:
        settings.REBAC_LOCAL_BACKEND_STORAGE = storage
    local = install_schema(schema)
    seed()
    local.write_relationships(GRANTS)
    return local


@pytest.fixture
def labelled(settings):
    """Threads show their label; display rows are on."""
    settings.REBAC_DISPLAY_FIELDS = True
    display.declare(ScopeThread, "label")
    yield
    display.declare(ScopeThread)


def shown_threads() -> Any:
    return ScopeThread.objects.with_actor(MEMBER).for_display()


def messages(thread_queryset: Any, *, action: str | None = None) -> Any:
    queryset = ScopeMessage.objects.with_actor(MEMBER).order_by("pk")
    if action is not None:
        queryset = queryset.with_action(action)
    return queryset.prefetch_related(Prefetch("thread", queryset=thread_queryset))


def thread_of(row: Any) -> Any:
    """``(pk, label, display row)`` of a message's thread, or None where none was delivered."""
    try:
        thread = row.thread
    except ScopeThread.DoesNotExist:
        return None
    return None if thread is None else (thread.pk, thread.label, display.is_display_row(thread))


def view(rows: list[Any]) -> dict[int, Any]:
    return {row.pk: thread_of(row) for row in rows}


def shown(pks: set[int]) -> dict[int, Any]:
    """Message ``pk`` with its thread as a display row."""
    threads = {1: 1, 2: 2, 3: 3, 4: 3}
    return {pk: (threads[pk], LABELS[threads[pk]], True) for pk in sorted(pks)}


def thread_statements(read_rows: Callable[[], Any]) -> tuple[Any, list[str]]:
    """What a read returns, and the statements of its second run that select threads."""
    read_rows()
    with CaptureQueriesContext(connection) as captured:
        found = read_rows()
    sql = [query["sql"] for query in captured.captured_queries]
    # A thread read selects the label; nothing else does.
    return found, [s for s in sql if '"testapp_scopethread"."label"' in s]


# ---------- The parents are those the evaluated policy admits ----------


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_display_rows_follow_the_parents_the_evaluated_policy_admits(settings, labelled, storage):
    """Message 3 is read through a link that the narrowed policy does not follow: its thread is
    no longer shown. Under either policy the label read is the library's, with no permission
    statement, and thread 1, which the member may not read, is shown as a display row."""
    install(settings, storage=storage)
    found = {}
    for inherit in (True, False):
        settings.REBAC_INHERIT_THROUGH_LINKS = inherit
        found[inherit], sql = thread_statements(lambda: view(list(messages(shown_threads()))))
        [label_read] = sql
        assert "rebac_" not in label_read and '"label"' in label_read, inherit
    assert found[True] == shown(PUBLISHED)
    assert found[False] == shown(NARROWED)
    assert found[False].items() <= found[True].items()
    # Without the request the threads are read by their scope, whole, under either policy.
    for inherit, pks in ((True, PUBLISHED), (False, NARROWED)):
        settings.REBAC_INHERIT_THROUGH_LINKS = inherit
        scoped, [scope] = thread_statements(
            lambda: view(list(messages(ScopeThread.objects.with_actor(MEMBER))))
        )
        assert "rebac_" in scope
        whole = {pk: None if pk == 1 else (pk, LABELS[pk], False) for pk in sorted(pks)}
        assert scoped == whole, inherit


def test_a_nested_level_admits_the_rows_the_narrowed_scope_read(settings, labelled):
    """Parts, their messages by their scope, the messages' threads as display rows: a message
    the narrowed policy does not grant is not read, so its thread is not shown."""
    install(settings)

    def read() -> dict[int, Any]:
        rows = (
            ScopePart.objects.with_actor(MEMBER)
            .order_by("pk")
            .prefetch_related(
                Prefetch("message", queryset=ScopeMessage.objects.with_actor(MEMBER)),
                Prefetch("message__thread", queryset=shown_threads()),
            )
        )
        found = {}
        for part in rows:
            try:
                message = part.message
            except ScopeMessage.DoesNotExist:
                message = None
            found[part.pk] = None if message is None else thread_of(message)
        return found

    settings.REBAC_INHERIT_THROUGH_LINKS = True
    assert read() == {**shown(PUBLISHED), 4: None}
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    assert read() == {**shown(NARROWED), 3: None, 4: None}


@pytest.mark.django_db(transaction=True)
def test_every_evaluation_path_reads_the_narrowed_parents(settings, labelled):
    """``list()`` and ``async for`` run the evaluation with its prefetches and deliver for the
    narrowed parents; ``iterator()`` and ``aiterator()`` chunks open no running evaluation, so
    their label prefetch is read by its scope, under the narrowed policy too."""
    install(settings)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    queryset = messages(shown_threads())

    async def evaluated() -> list[Any]:
        return [row async for row in queryset.all()]

    async def chunked() -> list[Any]:
        return [row async for row in queryset.aiterator(chunk_size=10)]

    by_scope = {1: None, 2: (2, "second", False)}
    assert view(list(queryset.all())) == shown(NARROWED)
    assert view(asyncio.run(evaluated())) == shown(NARROWED)
    assert view(list(queryset.all().iterator(chunk_size=10))) == by_scope
    assert view(asyncio.run(chunked())) == by_scope


# ---------- Field gates are declared names ----------


@pytest.mark.parametrize("schema", [GATED_KEY, GATED_LABEL], ids=["the key", "the label"])
def test_a_gate_disables_the_delivery_under_both_policies(settings, labelled, schema):
    """A gate on the parent's foreign key, or on the only display field, makes the request read
    by its scope, statement for statement, whatever the narrowing does to the gate's own
    expression: the gates the display rows consult are the policy's declared names, which the
    narrowed policy shares."""
    local = install(settings, schema)
    for inherit in (True, False):
        settings.REBAC_INHERIT_THROUGH_LINKS = inherit
        for definition in local.evaluated_schema().definitions:
            published = local.schema().get_definition(definition.resource_type)
            assert published is not None
            assert field_gated_actions(definition, "read") == field_gated_actions(published, "read")
        asked, asked_sql = thread_statements(lambda: view(list(messages(shown_threads()))))
        plain, plain_sql = thread_statements(
            lambda: view(list(messages(ScopeThread.objects.with_actor(MEMBER))))
        )
        assert asked_sql == plain_sql and "rebac_" in asked_sql[0], inherit
        assert asked == plain and not any(row and row[2] for row in asked.values()), inherit


# ---------- A generated name admits nothing ----------

GENERATED = ["lent(read)", "excluded(read)", "excluded(lent(read))"]


def forged(keys: list[Any]) -> Any:
    """The label queryset bound as Django binds a message's thread prefetch, to ``keys``."""
    pk = ScopeThread._meta.pk
    pairs = ColPairs("testapp_scopethread", [pk], [pk], ScopeMessage._meta.get_field("thread"))
    return shown_threads().filter(TupleIn(pairs, [(key,) for key in keys]))


@pytest.mark.parametrize("action", GENERATED)
@pytest.mark.parametrize("inherit", [True, False], ids=["published", "narrowed"])
def test_parents_scoped_under_a_generated_name_admit_nothing(settings, labelled, action, inherit):
    """A generated name is an unknown action at every entry: parents scoped under it are none,
    and rows recorded under it, whatever their keys, are not rows admitted to be read."""
    install(settings)
    settings.REBAC_INHERIT_THROUGH_LINKS = inherit
    assert list(messages(shown_threads(), action=action)) == []
    every = list(ScopeMessage._base_manager.order_by("pk"))
    with display.referring(MEMBER, ScopeMessage, action, every):
        # Read by its scope: the member may not read thread 1.
        assert list(forged([1])) == []
    with display.referring(MEMBER, ScopeMessage, "read", every):
        assert [(t.pk, display.is_display_row(t)) for t in forged([1])] == [(1, True)]
    assert not links._published.get()


# ---------- Parents admitted under an action the evaluated policy proves implies read ----------

# A note lends what the message it is linked to grants. ``linked`` is a stored relation to
# records of another type, a link. Published, ``read_inbox`` reaches a message's ``view``, which
# its thread grants as well, and does not imply ``read``; narrowed, a message lends of ``view``
# only its own ``reader`` (``lent(view)``), which is its ``read``: the narrowed policy proves
# ``read_inbox`` implies ``read``.
IMPLIED = """
definition auth/user {}
definition scope/thread {
    relation reader: auth/user
    permission read = reader
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation reader: auth/user
    permission view = reader + thread->read
    permission read = reader
}
definition lab/account {
    relation reader: auth/user
    permission read = reader
}
definition lab/note {
    relation linked: scope/message
    permission read = linked->read
    permission read_inbox = linked->view
}
"""


@pytest.fixture
def implied(settings):
    """Message 1 in thread 1, message 2 in thread 2; the member reads message 1 and thread 2.
    Note 1 (Alice's) is linked to message 1, note 2 (Carol's) to message 2. An account shows its
    name; the member reads no account."""
    settings.REBAC_DISPLAY_FIELDS = True
    local = install_schema(IMPLIED)
    ScopeThread._base_manager.bulk_create(
        [ScopeThread(id=1, label="one"), ScopeThread(id=2, label="two")]
    )
    ScopeMessage._base_manager.bulk_create(
        [ScopeMessage(id=1, thread_id=1), ScopeMessage(id=2, thread_id=2)]
    )
    LabAccount._base_manager.bulk_create(
        [LabAccount(id=1, code="a", name="Alice"), LabAccount(id=3, code="c", name="Carol")]
    )
    LabNote._base_manager.bulk_create([LabNote(id=1, owner_id=1), LabNote(id=2, owner_id=3)])
    local.write_relationships(
        [
            grant("scope/message", 1, "reader"),
            grant("scope/thread", 2, "reader"),
            grant("lab/note", 1, "linked", SubjectRef.of("scope/message", "1")),
            grant("lab/note", 2, "linked", SubjectRef.of("scope/message", "2")),
        ]
    )
    display.declare(LabAccount, "name")
    display.declare(ScopeThread, "label")
    yield local
    display.declare(LabAccount)
    display.declare(ScopeThread)


def inbox_notes() -> Any:
    accounts = LabAccount.objects.with_actor(MEMBER).for_display()
    return (
        LabNote.objects.with_actor(MEMBER)
        .with_action("read_inbox")
        .order_by("pk")
        .prefetch_related(Prefetch("owner", queryset=accounts))
    )


def owners_of(notes: Any) -> list[Any]:
    found = []
    for note in notes:
        try:
            owner = note.owner
        except LabAccount.DoesNotExist:
            owner = None
        found.append(
            None if owner is None else (owner.pk, owner.name, display.is_display_row(owner))
        )
    return found


def test_the_proof_reads_the_policy_the_parents_were_admitted_by(implied, settings):
    """Published, ``read_inbox`` admits both notes and does not imply ``read``: no owner is
    shown. Narrowed, it admits note 1 alone, and the narrowed policy proves it implies ``read``:
    Alice is shown. Read from the published policy, the proof would show nothing there."""
    settings.REBAC_INHERIT_THROUGH_LINKS = True
    assert not display.admits_reading(LabNote, "read_inbox")
    assert owners_of(inbox_notes()) == [None, None]
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    assert display.admits_reading(LabNote, "read_inbox")
    assert owners_of(inbox_notes()) == [(1, "Alice", True)]


@pytest.mark.parametrize(
    ("built", "read_under", "expected"),
    [(False, True, [(1, "Alice", True)]), (True, False, [None, None])],
    ids=["built narrowed, read published", "built published, read narrowed"],
)
def test_a_scope_built_under_one_policy_and_read_under_the_other(
    implied, settings, monkeypatch, built, read_under, expected
):
    """An eager scope keeps the policy it was built under, and its rows were admitted by it: what
    they were admitted to is what that policy proves. Decided where they are recorded instead,
    under the other value, the scope built published (both notes) would show Carol, the owner of
    a note the member may read under neither policy."""
    monkeypatch.setitem(app_settings._cache, "REBAC_INHERIT_THROUGH_LINKS", built)
    notes = inbox_notes().scoped()
    monkeypatch.setitem(app_settings._cache, "REBAC_INHERIT_THROUGH_LINKS", read_under)
    assert owners_of(notes) == expected


@pytest.mark.parametrize(
    ("counted", "listed", "expected"),
    [(False, True, [None, None]), (True, False, [(1, "Alice", True)])],
    ids=["counted narrowed, listed published", "counted published, listed narrowed"],
)
def test_a_queryset_counted_under_one_policy_is_listed_under_the_other(
    implied, monkeypatch, counted, listed, expected
):
    """A queryset that pins no scope is scoped under the policy in force at each evaluation:
    counted under one value and listed under the other, its rows are admitted, and refer to
    display rows, as the policy in force when it is listed says. Kept from the count, the
    narrowed scope would show Alice under the published policy, which proves nothing."""
    monkeypatch.setitem(app_settings._cache, "REBAC_INHERIT_THROUGH_LINKS", counted)
    notes = inbox_notes()
    assert notes.count() == (2 if counted else 1)
    monkeypatch.setitem(app_settings._cache, "REBAC_INHERIT_THROUGH_LINKS", listed)
    assert owners_of(notes) == expected


def test_a_generated_name_never_counts_though_the_narrowed_policy_proves_it(implied, settings):
    """The narrowed policy holds ``lent(view)`` on a message and proves it implies ``read``. It
    is still not an action: messages recorded under it lend no thread label, and messages scoped
    under it are none. Recorded under ``read``, the same messages lend it."""
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    policy = compiled._policy(implied)
    assert structural.implies(policy, "scope/message", "lent(view)", "read")
    for name in ("lent(view)", "excluded(view)", "excluded(lent(view))"):
        assert not display.admits_reading(ScopeMessage, name), name
    messages = list(ScopeMessage._base_manager.filter(pk=1))
    pk = ScopeThread._meta.pk
    pairs = ColPairs("testapp_scopethread", [pk], [pk], ScopeMessage._meta.get_field("thread"))
    binding = shown_threads().filter(TupleIn(pairs, [(1,)]))
    with display.referring(MEMBER, ScopeMessage, "lent(view)", messages):
        assert list(binding.all()) == []
    with display.referring(MEMBER, ScopeMessage, "read", messages):
        assert [(t.pk, display.is_display_row(t)) for t in binding.all()] == [(1, True)]
    assert list(ScopeMessage.objects.with_actor(MEMBER).with_action("lent(view)")) == []


# ---------- The display read and the published windows ----------


def test_the_display_read_runs_outside_every_published_window(settings, labelled, monkeypatch):
    """A window in which the published policy answers a generated name is opened by a scope
    and closed before its rows are read; no display decision or read runs inside one, and no
    window opens inside a display read, on a read that also scopes a generated name."""
    install(settings)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    seen: list[tuple[str, bool]] = []
    reading = [False]
    published_for = links._published_for
    deliverable, read = display.deliverable, display.read

    def recorded_window(actions: Any) -> Any:
        seen.append(("window", reading[0]))
        return published_for(actions)

    def recorded_deliverable(queryset: Any, actor: SubjectRef) -> Any:
        seen.append(("decide", links._published.get() or links.inherits_through_links()))
        return deliverable(queryset, actor)

    def recorded_read(*args: Any, **kwargs: Any) -> Any:
        seen.append(("read", links._published.get() or links.inherits_through_links()))
        reading[0] = True
        try:
            return read(*args, **kwargs)
        finally:
            reading[0] = False

    monkeypatch.setattr(links, "_published_for", recorded_window)
    monkeypatch.setattr(local_backend, "_published_for", recorded_window)
    monkeypatch.setattr(display, "deliverable", recorded_deliverable)
    monkeypatch.setattr(display, "read", recorded_read)
    assert list(messages(shown_threads(), action="lent(read)")) == []
    assert view(list(messages(shown_threads()))) == shown(NARROWED)
    assert ("decide", False) in seen and ("read", False) in seen
    assert ("window", False) in seen
    assert all(entry in {("decide", False), ("read", False), ("window", False)} for entry in seen)


# ---------- A scope that raised is built again ----------


def failing_once(monkeypatch: pytest.MonkeyPatch, *, kept: bool = False) -> list[int]:
    """The narrowing fails at its next call (every call, when ``kept``)."""
    real = links.narrowed_tagged
    calls: list[int] = []

    def narrowed_tagged(composed: Any) -> Any:
        calls.append(1)
        if kept or len(calls) == 1:
            raise links.NarrowingError("planted (rebac.E025)", kept=kept)
        return real(composed)

    monkeypatch.setattr(links, "narrowed_tagged", narrowed_tagged)
    return calls


def test_a_scope_whose_narrowing_failed_is_built_again(settings, labelled, monkeypatch):
    """A failure the next operation retries: the same queryset, evaluated again, is scoped by
    the narrowed policy. Before, it kept the decision of a scope it never added, read every
    message and admitted them all, so every thread was shown."""
    local = install(settings)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    failing_once(monkeypatch)
    queryset = messages(shown_threads())
    with pytest.raises(links.NarrowingError):
        list(queryset)
    assert view(list(queryset)) == shown(NARROWED)
    # The narrowing is kept per composition: forget it, and the next one fails once more.
    local.mark_schema_stale()
    failing_once(monkeypatch)
    counted = ScopeMessage.objects.with_actor(MEMBER)
    with pytest.raises(links.NarrowingError):
        counted.count()
    assert counted.count() == len(NARROWED)


def test_a_refused_narrowing_fails_closed_at_every_evaluation(settings, labelled, monkeypatch):
    install(settings)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    failing_once(monkeypatch, kept=True)
    queryset = messages(shown_threads())
    for _ in range(2):
        with pytest.raises(links.NarrowingError, match=r"rebac\.E025"):
            list(queryset)
        with pytest.raises(links.NarrowingError, match=r"rebac\.E025"):
            queryset.count()
        with pytest.raises(links.NarrowingError, match=r"rebac\.E025"):
            list(queryset.iterator(chunk_size=10))
    # The label queryset, evaluated alone, is read by its scope: it fails closed too.
    with pytest.raises(links.NarrowingError):
        list(forged([1]))


def test_any_scope_that_raised_is_built_again(settings, monkeypatch):
    """Not only a narrowing: any error while the scope is built leaves the queryset unscoped,
    and its next evaluation scopes it."""
    install(settings)
    real = rowscope.predicate
    calls: list[int] = []

    def predicate(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise SchemaError("planted")
        return real(*args, **kwargs)

    monkeypatch.setattr(rowscope, "predicate", predicate)
    queryset = ScopeMessage.objects.with_actor(MEMBER).order_by("pk")
    with pytest.raises(SchemaError, match="planted"):
        list(queryset)
    assert [row.pk for row in queryset] == sorted(PUBLISHED)


# ---------- The checks of both features ----------


def test_each_feature_check_is_reported_once_under_its_own_id(settings, monkeypatch):
    """With the setting off, a display declaration naming no field and a stored name the
    language cannot spell: ``rebac.W012`` and ``rebac.E026`` at every check, once each;
    ``rebac.W013``, a warning, only in a database check, once."""
    from django.core.checks import ERROR, WARNING, run_checks

    from rebac import checks as system_checks
    from rebac.schema.ast import Definition, Permission, PermRef, Schema
    from rebac.schema.parser import parse_zed

    install(settings)
    settings.REBAC_INHERIT_THROUGH_LINKS = False
    published = parse_zed(SCHEMA)
    stored = Schema(
        definitions=[
            d
            if d.resource_type != "scope/thread"
            else Definition(
                d.resource_type,
                d.relations,
                (*d.permissions, Permission("can-read", PermRef("reader"))),
            )
            for d in published.definitions
        ]
    )
    monkeypatch.setattr(system_checks, "_schema_on_alias", lambda using: stored)
    display.declare(ScopeThread, "label", "missing")
    try:
        for databases, expected in ((None, set()), (["default"], {("rebac.W013", WARNING)})):
            issues = [
                (issue.id, issue.level)
                for issue in run_checks(databases=databases)
                if issue.id in {"rebac.W012", "rebac.E025", "rebac.E026", "rebac.W013"}
            ]
            assert sorted(issues) == sorted(
                {("rebac.W012", WARNING), ("rebac.E026", ERROR), *expected}
            ), databases
    finally:
        display.declare(ScopeThread)
