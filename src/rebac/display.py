"""Fields a row shows to whoever reads a row that refers to it (proposal 0020).

A list of messages shows each author's name. Today ``rebac_select_related``
joins the author rows and then asks, for the batch, whether the actor may read
each one, and raises if it may not: the list cannot be shown to someone who may
read the messages and not the accounts. Most applications mean something
narrower: an account's name is visible wherever the account is referred to,
and the account itself is not.

A model says so by declaring display fields

    class Account(RebacMixin, models.Model):
        class Meta:
            rebac_resource_type = "app/account"
            rebac_display_fields = ("name", "initials")

or, for a model that cannot carry the option, ``display.declare(Model, "name")``.
With ``REBAC_DISPLAY_FIELDS`` set, a related row that ``rebac_select_related``
loaded is then delivered with its key and its display fields, every other field
hidden as a denied field is (``redacted_fields`` names them), in one of two ways:

- ``"fallback"``: the batch is checked as before. A row the actor holds is
  delivered whole; a row it does not hold is delivered with display fields
  instead of raising.
- ``True``: a row of a model that declares display fields is not checked at
  all, and is always delivered with display fields only. No permission
  statement is issued for the relation. Read the row itself to see more.

A model that declares none behaves as before in both. A display field that
carries a field gate (``read__<field>``) is not displayed: the gate decides it,
under whatever policy is current. A hidden foreign key reads as absent, so the
rows that hung off it are no longer reached from the display row; the guard
checks what is still reached.

A display field is a plain column of the model's own: a foreign key or a
one-to-one is refused, for joined and prefetched display rows alike. A display
row always shows its primary key, so a model whose primary key (or a
multi-table parent's) is a relation other than a parent link declares none:
following a one-to-one primary key from a display row would read the row it
names, whole. A parent link leads to the row's own parent part, which Django
builds from the display row's values without a statement. With only plain
columns and such keys shown, and every hidden foreign key reading as absent, no
field a display row shows leads to another row. Its other relations, followed
lazily, load as they do from any row of this library; a display row adds nothing
to that and takes nothing from it. A reverse one-to-one is loaded by Django's
descriptor through the base manager, whole and with no permission statement
(``rebac.W003`` lists such relations, from the model that declares them); a
reverse foreign key's or a many-to-many's related manager reads by its scope.
To show a related row's label, declare that model's own display fields and
prefetch it with ``for_display()``. A declaration that breaks these rules
(``declaration_problems``) is reported by system check ``rebac.E026`` whatever
``REBAC_DISPLAY_FIELDS`` is, since it is a hole the moment the setting is
turned on, and refused by the first read that consults it.

Only a ``RebacMixin`` model can declare display fields. Its ``save()``,
``asave()`` and ``save_base()`` (a raw one included) leave out of a save that
names no fields a hidden field that holds its placeholder (``None``, or what
the field's descriptor makes of it when read, such as a ``FieldFile`` with no
name), where a plain model's would write it back as NULL, refuse one given a
value; no insert copies the display row (a save as a new row,
``bulk_create()``); its writes that name a hidden field refuse it
(``rebac.field_visibility.refuse_hidden_fields``). The parent instance a
multi-table display row's parent link gives is Django's, built from the display
row's values: its hidden fields are ``None`` there and nothing marks them, so
saving that instance writes them (a field-redacted multi-table row's does the
same).

Display rows through a prefetch (proposal 0024)
-----------------------------------------------

An application that loads a foreign key with a prefetch rather than a join asks
for display rows on the prefetch's queryset

    Prefetch("author", queryset=Account.objects.with_actor(actor).for_display())

Under either setting (``"fallback"`` and ``True`` mean the same here: the request
decides), the rows are then delivered as display rows without a permission
statement when ``deliverable`` finds all of its conditions. In short: the
queryset is Django's plain prefetch of a forward foreign key (or one-to-one) to
its model's primary key, and every key it asks for is a value the database
returned for that foreign key on a row that an actor-scoped read for the same
actor admitted to be read in the queryset evaluation that is running, in the
same thread and task. A row is admitted to be read when its scope's action is
its type's read action, or an action the policy that scope was built under
proves implies it (``admits_reading``, ``rebac.structural.implies``): an inbox
that lists messages under ``read_inbox`` shows the labels they refer to when
``read_inbox`` implies ``read``. The library then reads the rows itself
(``read``): a bare queryset of the model, on the parents' database, by those
values, selecting the primary key and the display columns alone. The
queryset's own conditions are never executed, and no manager of the
application takes part.

A reader is shown, for a foreign key it holds on a row it was admitted to read
and whose type gates no read of that field, the display fields of the row that
key names, and of no other row. A delivered row is built with every hidden
column given as ``None``: the model's own code that runs while it is built
(``__init__``, ``post_init`` receivers) sees ``None`` there, and nothing is
deferred, so no statement loads a hidden column. A row the reader may read
whole is delivered as a display row too: switching a prefetch to
``for_display()`` loses the other fields of those rows. A display row equals,
and hashes as, the same row read whole (Django compares model and key), so a
set, a deduplication or a dict keyed by instance can hold one in place of the
other.

Besides Django's binding, the queryset may carry what changes no condition,
join, column or order of the library's read: its actor, ``with_action()``,
``on_field_deny()`` (the deny mode the hidden fields are marked with),
``only()`` and ``defer()`` (the library reads its own columns),
``reverse()``, ``fetch_mode()``, ``prefetch_related()`` (whose lookups are read
below the display rows, by their scope) and ``using()`` of the parents'
database. Anything else is read as it would be without the request: by its
permission statement, whole rows of those the actor may read. That includes a
reverse one-to-one, a foreign key to another column than the primary key, a
proxy or subclass of the target on the prefetch, a target a router sends to
another database than its parents', and any queryset with conditions, ordering,
annotations, projections, slices or combinations of its own. A delivered row is
never itself admitted, so nothing is delivered through it in turn.

Whether a queryset delivers display rows is decided by an evaluation that
scopes its query (``RebacQuerySet._fetch_all``), and serves that evaluation
alone: the queryset keeps nothing of it, so a clone, the same queryset under
another actor, bypass or database, and the next evaluation after one that
raised are decided again. An embedding of its query, a count and its own
``iterator()`` are decided where they run and never delivered. A decision that
is not a delivery reads by the permission statement. A queryset whose query
already carries a scope for the same decision keeps that query: after its own
``count()``, ``exists()``, ``aggregate()``, ``iterator()`` or ``aiterator()``,
and for an eager ``scoped()`` queryset and its clones, an evaluation under the
same actor, bypass and database reuses it at the cost of one comparison and
reads by its permission statement, decided neither for display nor for
structural delivery.

What is admitted (``referring``, ``admit``): the rows of an evaluation that read
model instances built from the database by Django's own iterable, with no
``extra(select=...)`` and no set combination (whose rows may come from another
table), admitted to be read. Rows read through a many-to-many prefetch are
therefore never admitted: Django builds that prefetch with ``extra(select=...)``.
Whether rows were admitted to be read is decided where the queryset's scope is
built, and carried by its query with its decision: a policy that changes before a delivery
is decided does not change what a recorded row was admitted to, as it does not
for a row admitted under the read action itself. What a row refers to is
recorded when it is admitted, from the values the database returned for it
(``RebacMixin.from_db`` keeps them), never from its attributes: nothing assigned
to the row after it was built (a related manager's known objects, a prefetch's
``to_attr``, ``__init__`` or a ``post_init`` receiver, any later code) changes
what it refers to, and a deferred key, not returned, refers to nothing. What
remains trusted is what produces the returned values: the database, a custom
field's ``from_db_value``, and a model's own ``from_db`` override that changes
them before the library's records them.

The record of an evaluation serves, for its actor, code that runs with it in its
context, in the thread that opened it and in the asyncio task that opened it
(none, for an evaluation run outside a task), until the evaluation ends. A
thread started inside the evaluation starts with an empty context on the default
build, and with a copy of its starter's on a free-threaded build or when it is
given one (``copy_context().run``, ``Thread(context=...)``); a task
(``create_task``, ``asyncio.run``) runs with a copy; a context copied inside the
evaluation may be run after it has ended. In
none of them does the record refer to anything, so a ``for_display()`` queryset
evaluated there is read by its scope. Code that such a task hands back to the
evaluation's thread is served: asgiref's ``async_to_sync``, called inside the
evaluation, runs its coroutine in a thread of its own, and a ``sync_to_async``
call in it (thread-sensitive, the default) runs back in the evaluation's
thread, outside any task, with the evaluation's context. Thread and task
identity cannot tell that code from the evaluation's own, and the record
serves it for the same actor until the evaluation ends. The chunks of
``iterator()`` and ``aiterator()`` open no record and admit none of their
rows: their to-one prefetches are delivered only inside a running evaluation
for the same actor, for keys its admitted rows hold, and are read by their
scope otherwise.

``referring`` and ``admit`` record rows as admitted without a permission
statement and without a bypass audit: code that holds rows and calls them makes
any foreign-key target those rows hold displayable to the actor it names, while
the block runs. They are the library's own seam for its querysets and for
trusted framework code built on it, never a door for application code.

With the setting on, every actor-scoped queryset whose scope is built decides
whether its action admits its rows to be read (``admits_reading``), whether or
not a prefetch uses the answer: for the read action, one comparison; for any
other, the policy the backend evaluates and a kept proof are looked up, about
12 microseconds on the test suite's in-process policy, and the first read of a
policy and action runs the proof. It adds no statement to the read, on an
in-process policy or a stored one. ``implies`` never proves a
recursive read action, not even ``browse = read``: under an action other than
the read action, rows of a type whose read action is recursive refer to
nothing.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, cast

from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models.expressions import ColPairs
from django.db.models.fields.tuple_lookups import TupleIn
from django.db.models.sql.where import AND, WhereNode

from ._id import resource_id_attr
from .types import FieldDenyMode, SubjectRef

_declared: dict[type[models.Model], tuple[str, ...]] = {}
# Declarations already checked against their model.
_checked: set[tuple[type[models.Model], tuple[str, ...]]] = set()


def mode() -> str:
    """``"always"``, ``"fallback"`` or empty for off."""
    from .conf import app_settings

    value = app_settings.REBAC_DISPLAY_FIELDS
    if value is True or value == "always":
        return "always"
    return "fallback" if value == "fallback" else ""


def declare(model: type[models.Model], *fields: str) -> None:
    """Declare ``model``'s display fields, for a model whose ``Meta`` cannot be edited.

    Declaring none withdraws an earlier declaration.
    """
    if fields:
        _declared[model] = tuple(fields)
    else:
        _declared.pop(model, None)


def _declaration(model: type[models.Model]) -> tuple[str, ...]:
    found = _declared.get(model)
    if found is None:
        found = tuple(getattr(model._meta, "rebac_display_fields", ()) or ())
    return found


def fields(model: type[models.Model]) -> frozenset[str]:
    """The fields of ``model`` shown on a display row; empty when it declares none."""
    return _sides(model)[0]


def declaration_problems(model: type[models.Model]) -> list[str]:
    """What is wrong with ``model``'s display declaration, one sentence each; empty when nothing is.

    The one statement of what a declaration may name: system check ``rebac.E026``
    reports these at startup, whatever ``REBAC_DISPLAY_FIELDS`` is, and the first
    read that consults the declaration refuses it. Only a ``RebacMixin`` model may
    declare display fields, its primary key (and each multi-table parent's) must
    not be a relation other than a parent link, since every display row shows it,
    and each display field must be a field of the model, one of its own columns,
    and not a relation.
    """
    declared = _declaration(model)
    if not declared:
        return []
    from .mixins import RebacMixin

    if not issubclass(model, RebacMixin):
        return [
            f"{model.__name__}: display fields need a RebacMixin model, whose save() "
            "leaves hidden fields alone."
        ]
    problems: list[str] = []
    for owner in (model, *model._meta.all_parents):
        key = owner._meta.pk
        if (
            key is not None
            and key.is_relation
            and not getattr(key.remote_field, "parent_link", False)
        ):
            problems.append(
                f"{model.__name__}: the primary key {key.name!r} of {owner.__name__} is a "
                "relation, which every display row shows and which leads to the row it names. "
                "Display fields need a primary key that is a plain column or a multi-table "
                "parent link."
            )
    concrete = {field.name for field in model._meta.concrete_fields}
    for name in declared:
        try:
            field = model._meta.get_field(name)
        except FieldDoesNotExist:
            problems.append(
                f"{model.__name__}: display field {name!r} is not a field of the model."
            )
            continue
        if field.name not in concrete:
            problems.append(
                f"{model.__name__}: display field {name!r} is not one of the model's own columns."
            )
        elif field.is_relation:
            problems.append(
                f"{model.__name__}: display field {name!r} is a relation, and a display field is "
                "a plain column. Declare the related model's own display fields and prefetch it "
                "with for_display()."
            )
    return problems


def _sides(model: type[models.Model]) -> tuple[frozenset[str], frozenset[str]]:
    """``(shown, hidden)`` field names of ``model``'s display rows, under the current policy."""
    declared = _declaration(model)
    if not declared:
        return frozenset(), frozenset()
    from .field_visibility import gated_read_fields

    concrete = {field.name: field for field in model._meta.concrete_fields}
    if (model, declared) not in _checked:
        problems = declaration_problems(model)
        if problems:
            raise ImproperlyConfigured(problems[0])
        _checked.add((model, declared))
    # A gated field is decided by its gate, never by this declaration: read from the
    # policy each time, since a later policy may gate a field this one does not.
    shown = frozenset(declared) - gated_read_fields(model)
    identity = {resource_id_attr(model)}
    hidden = frozenset(
        name
        for name, field in concrete.items()
        if not field.primary_key and name not in shown and name not in identity
    )
    return shown, hidden


def deliver(
    rows: Iterable[models.Model],
    *,
    model: type[models.Model],
    actor: SubjectRef,
    field_mode: FieldDenyMode,
) -> None:
    """Make ``rows`` display rows: their key and display fields, the rest hidden.

    A hidden column is set as a denied field is. A hidden foreign key is set
    through its column, which drops the related object this row had loaded and
    touches nothing on that object: a one-to-one assigned through its
    descriptor would also clear the other side's link back to this row.
    """
    from .field_visibility import (
        _remember_resource_id,
        _scrub_loaded_value,
        mark_denied_fields,
        redacted_fields,
        runtime_field_deny_mode,
    )

    _shown, hidden = _sides(model)
    runtime = runtime_field_deny_mode(field_mode)
    denied: FieldDenyMode = "omit" if runtime == "omit" else "redact"
    relations = {
        name: model._meta.get_field(name)
        for name in hidden
        if model._meta.get_field(name).is_relation
    }
    columns = hidden - relations.keys()
    for row in rows:
        row._rebac_actor = actor  # type: ignore[attr-defined]
        row._rebac_field_deny = field_mode  # type: ignore[attr-defined]
        mark_denied_fields(row, columns, mode=denied)
        if relations:
            _remember_resource_id(row)
            for name, field in relations.items():
                setattr(row, cast(Any, field).attname, None)
                _scrub_loaded_value(row, name)
            row._rebac_redacted_fields = redacted_fields(row) | relations.keys()  # type: ignore[attr-defined]
            if denied == "omit":
                omitted: frozenset[str] = getattr(row, "_rebac_omitted_fields", None) or frozenset()
                row._rebac_omitted_fields = frozenset(omitted) | relations.keys()  # type: ignore[attr-defined]
        row._rebac_display_only = True  # type: ignore[attr-defined]
        # Nothing is left for the field gates to decide on this row.
        row._rebac_visibility_marker = (actor, runtime)  # type: ignore[attr-defined]


def is_display_row(instance: models.Model) -> bool:
    """Whether ``instance`` was delivered with its display fields only."""
    return bool(getattr(instance, "_rebac_display_only", False))


# ---------- Display rows through a prefetch (proposal 0024) ----------


def _here() -> tuple[int, Any]:
    """The thread and the asyncio task running now (``None`` outside a running loop)."""
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@dataclass
class _Referring:
    """What the rows one running queryset evaluation admitted for one actor refer to.

    ``rows`` maps a model to ``(database, foreign key column)`` to the values the
    database returned there for its rows admitted to be read (``admits_reading``,
    decided where they were admitted), by type and value. The record serves the
    thread and task that opened it alone: a thread or task started inside the
    evaluation carries a copy of the context, and the copy refers to nothing
    there; code such a task runs back in the opening thread, outside any task
    (asgiref's ``sync_to_async`` under ``async_to_sync``), is served.
    """

    actor: SubjectRef
    rows: dict[
        type[models.Model],
        dict[tuple[str | None, str], dict[tuple[type[Any], Any], Any]],
    ] = dataclass_field(default_factory=dict)
    opened_by: tuple[int, Any] = dataclass_field(default_factory=_here)
    # Set when the evaluation that opened it ends: a context copied before
    # then and run afterwards finds a record that serves nothing.
    closed: bool = False

    def serves(self, actor: SubjectRef) -> bool:
        """Whether this record is ``actor``'s, open, in the thread and task that opened it."""
        if self.closed or self.actor != actor:
            return False
        thread, task = _here()
        return thread == self.opened_by[0] and task is self.opened_by[1]


_referring: ContextVar[_Referring | None] = ContextVar("rebac_display_referring", default=None)


def tracks(queryset: Any) -> bool:
    """Whether the rows ``queryset`` fetches may refer: the setting is on, and their columns are
    their stored values (Django's own iterable, no ``extra(select=...)``, no set combination)."""
    from django.db.models.query import ModelIterable

    if not mode() or queryset._iterable_class is not ModelIterable:
        return False
    query = queryset.query
    return not query.extra and not query.combined_queries


@contextmanager
def referring(
    actor: SubjectRef,
    model: type[models.Model],
    action: str,
    rows: list[Any],
    *,
    reads: bool | None = None,
) -> Iterator[_Referring]:
    """Admit ``rows`` for the prefetches that run inside the block.

    ``rows`` were just read by a queryset scoped for ``actor`` and ``action``.
    ``reads`` says whether that scope admitted them to be read
    (``admits_reading``), as the queryset decided it where its scope was
    built; ``None`` decides it now. Rows not admitted to be read refer to
    nothing. An evaluation inside another one for the same actor, in the same
    thread and task, adds its rows to the one already open; any other opens its
    own for its block, and the rows it admits are never the outer one's.

    Trusted: nothing checks that ``rows`` were read under that scope, and no
    bypass is audited. Called with rows of its own, code makes any foreign-key
    target they hold displayable to ``actor`` inside the block. It is the seam
    of the library's querysets and of trusted framework code, not of
    application code.
    """
    current = _referring.get()
    if current is not None and current.serves(actor):
        admit(actor, model, action, rows, reads=reads)
        yield current
        return
    opened = _Referring(actor)
    if reads if reads is not None else admits_reading(model, action):
        _admit(opened, model, rows)
    token = _referring.set(opened)
    try:
        yield opened
    finally:
        opened.closed = True
        _referring.reset(token)


def admit(
    actor: SubjectRef,
    model: type[models.Model],
    action: str,
    rows: list[Any],
    *,
    reads: bool | None = None,
) -> None:
    """Add rows read inside a running evaluation for ``actor`` to it: a nested level's own parents.

    ``reads`` as for ``referring``, and trusted as it is: rows given here are
    not checked, and no bypass is audited.
    """
    current = _referring.get()
    if current is None or not current.serves(actor):
        return
    if reads if reads is not None else admits_reading(model, action):
        _admit(current, model, rows)


def admits_reading(model: type[models.Model], action: str) -> bool:
    """Whether rows of ``model`` a scope admits under ``action`` are admitted to be read.

    They are when ``action`` is the model's read action, the one a plain read of
    it is scoped by, or when the policy the backend evaluates proves that whoever
    is granted ``action`` on a row of the model's type is granted that read action
    (``rebac.structural.implies``, proposal 0022). The proof is kept per policy;
    nothing but the read action itself is proven while the policy carries an
    override, nor about a type that reads a name two ways.

    A queryset decides this where its scope is built (``RebacQuerySet._scope_query``)
    and its query carries the answer with its decision, to the rows it admits: what
    a recorded row was admitted to is what the policy that admitted it said,
    whatever the policy is when a delivery is decided.
    """
    read = str(getattr(model._meta, "rebac_default_action", "read"))
    if action == read:
        return True
    from . import structural
    from .resources import model_resource_type

    resource_type = model_resource_type(model)
    if not resource_type:
        return False
    policy = structural._policy()
    return policy is not None and structural.implies(policy, resource_type, action, read)


def _admit(referring: _Referring, model: type[models.Model], rows: list[Any]) -> None:
    """Record what ``rows``, admitted to be read, refer to: the foreign-key values the
    database returned for them.

    They are the values ``RebacMixin.from_db`` keeps (``_rebac_loaded_values``),
    never the row's attributes: Django and the caller may set those after the
    row is built (a related manager's known objects, a prefetch's ``to_attr``,
    any assignment). A deferred key was not returned and does not refer; a row
    the database did not build refers to nothing.
    """
    columns = [
        cast(Any, field).attname
        for field in model._meta.concrete_fields
        if field.is_relation and (field.many_to_one or field.one_to_one)
    ]
    held = referring.rows.setdefault(model, {})
    for row in rows:
        if not isinstance(row, models.Model):
            continue
        returned = getattr(row, "_rebac_loaded_values", None)
        if type(returned) is not dict:
            continue
        for column in columns:
            value = returned.get(column)
            if value is None:
                continue
            try:
                held.setdefault((row._state.db, column), {}).setdefault((type(value), value), value)
            except TypeError:
                continue


def deliverable(queryset: Any, actor: SubjectRef) -> list[Any] | None:
    """The keys a ``for_display()`` queryset's rows may be read by without a permission statement.

    They are the admitted rows' own foreign-key values, in the order the
    queryset asks for them; ``None`` means the queryset is read by its
    permission statement, as it would be without the request. Every one of
    these must hold, read from the queryset as it is about to be fetched as
    model instances (``RebacQuerySet._fetch_all``):

    - the setting is on; the model declares display fields its policy does
      not gate, and its identity is its primary key;
    - the queryset reads model instances of its own table and nothing else: no
      projection, annotation, extra selection, join, distinct, ordering of the
      caller's (``order_by``; the model's ``Meta.ordering`` is not the caller's,
      and a read by key needs no order), locking, slice or combination, and
      its WHERE clause is exactly one
      condition, not negated: the binding Django writes for the prefetch of a
      forward foreign key or one-to-one, ``TupleIn`` over ``ColPairs`` that
      names the relation, with the target's primary key as both its column and
      its source, every value a literal;
    - the relation is not a multi-table parent link, targets the primary key
      (no ``to_field``), and its target model is the queryset's model itself,
      not a proxy or a subclass of it;
    - an evaluation is running for the same actor, opened in this thread and
      task (``referring``), and every key asked for, compared with its type, is
      a value the database returned for that foreign key on a row it admitted
      (``_admit``): of the relation's model or a proxy or subclass of it,
      admitted to be read (``admits_reading``: under that type's read action,
      or an action the policy that admitted it proves implies it), on the
      queryset's database, where that type declares no read gate on the field.

    Nothing here issues a statement: a deferred foreign key is not loaded.
    """
    from django.db.models.query import ModelIterable

    if not mode() or queryset._iterable_class is not ModelIterable:
        return None
    referring = _referring.get()
    if referring is None or not referring.serves(actor):
        return None
    model = queryset.model
    query = queryset.query
    if not _reads_rows_alone(query, model):
        return None
    conditions = _conditions(query.where)
    if len(conditions) != 1:
        return None
    asked = _asked(conditions[0], query, model)
    if asked is None:
        return None
    relation, requested = asked
    if not fields(model) or not _identity_is_key(model):
        return None
    referred = _referred(referring, relation, queryset.db)
    if not all(key in referred for key in requested):
        return None
    return [referred[key] for key in requested]


def read(model: type[models.Model], keys: list[Any], *, using: str) -> list[models.Model]:
    """The rows of ``model`` that ``keys`` name, with their key and display fields alone.

    The library's own statement, built from a bare ``QuerySet`` of the model
    that no manager of the application touches: on ``using`` (the database the
    parents were admitted from), by primary key, with no other condition and no
    ordering, selecting the primary keys (a multi-table model's parent keys as
    well) and the display columns and nothing else. The keys are the admitted
    rows' own values (``deliverable``).

    Each row is built by ``model.from_db`` with every column given, the hidden
    ones as ``None``: nothing is deferred while the model's own code runs
    (``__init__``, ``post_init`` receivers), so it reads ``None`` for a hidden
    field and no statement loads one.
    """
    if not keys:
        return []
    shown, _hidden = _sides(model)
    columns = [field.attname for field in model._meta.concrete_fields]
    selected = [
        field.attname
        for field in model._meta.concrete_fields
        if field.primary_key or field.name in shown
    ]
    found: Iterable[tuple[Any, ...]] = (
        models.QuerySet(model=model, using=using)
        .filter(pk__in=keys)
        .order_by()
        .values_list(*selected)
    )
    rows: list[models.Model] = []
    for values in found:
        given = dict(zip(selected, values, strict=True))
        rows.append(model.from_db(using, columns, [given.get(column) for column in columns]))
    return rows


def _reads_rows_alone(query: Any, model: type[models.Model]) -> bool:
    """Whether ``query`` reads whole rows of its own table with nothing of the caller's added.

    The model's own ``Meta.ordering`` is not the caller's: Django applies it when the
    statement is compiled, and this query is never compiled when it delivers (``read``
    orders nothing). An ordering the caller asked for (``order_by``) is. Django's prefetch
    of a forward to-one clears both before the queryset is evaluated; a binding evaluated
    as it stands keeps them.
    """
    if (
        query.combined_queries
        or query.is_sliced
        or query.distinct
        or query.select_for_update
        or query.annotations
        or query.extra
        or query.extra_tables
        or query.values_select
        or query.select
        or query.select_related
        or query.group_by is not None
        or query.order_by
        or query.extra_order_by
    ):
        return False
    # A join Django trimmed stays in the map, unreferenced.
    base = _base_alias(query, model)
    return all(alias == base or not query.alias_refcount.get(alias) for alias in query.alias_map)


def _base_alias(query: Any, model: type[models.Model]) -> str:
    """The alias of ``query``'s own table, without creating it (``Query.base_table`` caches)."""
    return next(iter(query.alias_map), model._meta.db_table)


def _conditions(where: WhereNode) -> list[Any]:
    """The conditions every row must meet, opened through ``AND``; anything else is one condition."""
    found: list[Any] = []
    pending: list[Any] = [where]
    while pending:
        node = pending.pop()
        if type(node) is WhereNode and node.connector == AND and not node.negated:
            pending.extend(node.children)
        else:
            found.append(node)
    return found


def _asked(
    condition: Any, query: Any, model: type[models.Model]
) -> tuple[models.ForeignKey[Any, Any], list[tuple[type[Any], Any]]] | None:
    """``(relation, keys asked for)`` of a forward to-one prefetch's binding, else ``None``."""
    if type(condition) is not TupleIn or type(condition.lhs) is not ColPairs:
        return None
    pairs = condition.lhs
    relation = pairs.output_field
    pk = model._meta.pk
    if (
        not isinstance(relation, models.ForeignKey)
        or getattr(relation.remote_field, "parent_link", False)
        or relation.related_model is not model
        or relation.target_field is not pk
        or pairs.alias != _base_alias(query, model)
        or list(pairs.targets) != [pk]
        or list(pairs.sources) != [pk]
    ):
        return None
    values = condition.rhs
    if type(values) not in (list, tuple) or any(
        type(item) not in (list, tuple) or len(item) != 1 for item in values
    ):
        return None
    literals = [item[0] for item in values]
    if any(hasattr(value, "resolve_expression") or hasattr(value, "as_sql") for value in literals):
        return None
    try:
        # NULL is equal to nothing: Django drops it from the statement.
        requested = list(dict.fromkeys((type(v), v) for v in literals if v is not None))
    except TypeError:
        return None
    return relation, requested


def _referred(
    referring: _Referring, relation: models.ForeignKey[Any, Any], using: str
) -> dict[tuple[type[Any], Any], Any]:
    """The values ``relation`` held on rows ``referring`` admitted to be read, by type and value."""
    from .field_visibility import gated_read_fields

    holder = relation.model
    referred: dict[tuple[type[Any], Any], Any] = {}
    if not isinstance(holder, type):
        return referred
    for admitted, columns in referring.rows.items():
        if not issubclass(admitted, holder):
            continue
        # A read gate on the field decides it, never this delivery.
        if relation.name in gated_read_fields(admitted):
            continue
        for key, value in columns.get((using, relation.attname), {}).items():
            referred.setdefault(key, value)
    return referred


def _identity_is_key(model: type[models.Model]) -> bool:
    """Whether ``model``'s identity is its primary key: the value its parents hold."""
    attr = resource_id_attr(model)
    pk = model._meta.pk
    return attr == "pk" or (pk is not None and attr in (pk.name, pk.attname))
