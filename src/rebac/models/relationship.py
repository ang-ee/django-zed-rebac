"""Relationship — Tier 3 core REBAC store.

Wire-shape mirrors `authzed.api.v1.Relationship` exactly. Renames are breaking.

Two LocalBackend storage shapes coexist:

  - ``Relationship`` (denormalized) — the historical shape with four wide
    CharField columns. Default in 0.4.
  - ``RelationshipRegistry`` (registry) — same wire-shape, but the four
    string columns become two integer FKs into ``RebacResource``. Opt-in
    via ``REBAC_LOCAL_BACKEND_STORAGE='registry'``.

Both tables ship on disk; ``rebac.models.active_relationship_model()``
returns the one selected by the setting. The wire shape (``RelationshipTuple``
+ string kwargs to the active manager) is invariant across modes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, cast

from django.db import models, router
from django.db.models import F, Q

from ..conf import app_settings
from ..errors import RelationshipReadError
from ..types import ObjectRef, RelationshipTuple, SubjectRef

WIRE_VALUE_FIELDS = (
    "resource_type",
    "resource_id",
    "relation",
    "subject_type",
    "subject_id",
    "optional_subject_relation",
    "caveat_name",
)

INDEX_PROJECTION_FIELDS = (
    "resource_type",
    "resource_id",
    "relation",
    "subject_type",
    "subject_id",
    "subject_relation",
    "caveat_name",
    "caveat_context",
    "expires_at",
)

_REGISTRY_WIRE_FIELD_MAP = {
    "resource_type": "resource_fk__resource_type",
    "resource_id": "resource_fk__resource_id",
    "subject_type": "subject_fk__resource_type",
    "subject_id": "subject_fk__resource_id",
}

_engine_tuple_owner: ContextVar[bool] = ContextVar("rebac_engine_tuple_owner", default=False)


@contextmanager
def engine_tuple_write() -> Iterator[None]:
    """The backend's enclosing tuple owner already captures this ORM write."""
    token = _engine_tuple_owner.set(True)
    try:
        yield
    finally:
        _engine_tuple_owner.reset(token)


def _tuple_of(row: Relationship | RelationshipRegistry) -> RelationshipTuple:
    return RelationshipTuple(
        resource=ObjectRef(row.resource_type, row.resource_id),
        relation=row.relation,
        subject=SubjectRef.of(row.subject_type, row.subject_id, row.optional_subject_relation),
        caveat_name=row.caveat_name,
        caveat_context=row.caveat_context or {},
        expires_at=row.expires_at,
    )


def projected_tuples(rows: models.QuerySet[Any]) -> Iterable[RelationshipTuple]:
    """Stream the wire identity from either storage shape for old-state capture."""
    for row in rows.iterator(chunk_size=1000):
        yield RelationshipTuple(
            resource=ObjectRef(row["resource_type"], row["resource_id"]),
            relation=row["relation"],
            subject=SubjectRef.of(row["subject_type"], row["subject_id"], row["subject_relation"]),
        )


def _owned_instance_save(
    row: Relationship | RelationshipRegistry, save: Callable[[], None], using: str | None
) -> None:
    if _engine_tuple_owner.get():
        save()
        return
    from ..backends.local import mark_relationships_changed
    from ..index.maintain import tuple_owner

    using = using or router.db_for_write(type(row), instance=row)
    with tuple_owner(using) as maintenance:
        if maintenance is not None and not row._state.adding and row.pk is not None:
            old = type(row)._base_manager.using(using).filter(pk=row.pk).first()
            if old is not None:
                maintenance.capture_old(tuples=[_tuple_of(old)])
        save()
        if maintenance is not None:
            maintenance.changed(tuples=[_tuple_of(row)])
    mark_relationships_changed()


def _owned_instance_delete(
    row: Relationship | RelationshipRegistry,
    delete: Callable[[], tuple[int, dict[str, int]]],
    using: str | None,
) -> tuple[int, dict[str, int]]:
    if _engine_tuple_owner.get():
        return delete()
    from ..backends.local import mark_relationships_changed
    from ..index.maintain import tuple_owner

    using = using or router.db_for_write(type(row), instance=row)
    with tuple_owner(using) as maintenance:
        if maintenance is not None:
            maintenance.capture_old(tuples=[_tuple_of(row)])
        result = delete()
        if maintenance is not None:
            maintenance.changed()
    if result[0]:
        mark_relationships_changed()
    return result


class RelationshipQuerySet(models.QuerySet["Relationship"]):
    """Mode-agnostic queryset helpers for denormalized relationship rows."""

    def delete(self) -> tuple[int, dict[str, int]]:
        return _owned_tuple_delete(self, super().delete)

    def update(self, **kwargs: Any) -> int:
        raise NotImplementedError(
            "Relationship queryset update() is unsupported; use delete_relationships() "
            "and write_relationships() to change tuples."
        )

    def index_projection(self) -> models.QuerySet[Any]:
        return cast(
            models.QuerySet[Any],
            self.annotate(subject_relation=F("optional_subject_relation")).values(
                *INDEX_PROJECTION_FIELDS
            ),
        )

    def for_resource(self, resource_type: str, resource_id: str) -> RelationshipQuerySet:
        return self.filter(resource_type=resource_type, resource_id=resource_id)

    def for_subject(
        self,
        subject_type: str,
        subject_id: str,
        optional_relation: str | None = None,
    ) -> RelationshipQuerySet:
        qs = self.filter(subject_type=subject_type, subject_id=subject_id)
        if optional_relation is not None:
            qs = qs.filter(optional_subject_relation=optional_relation)
        return qs

    def order_by_resource(self) -> RelationshipQuerySet:
        return self.order_by(
            "resource_type",
            "resource_id",
            "relation",
            "subject_type",
            "subject_id",
            "optional_subject_relation",
            "caveat_name",
        )

    def order_by_subject(self) -> RelationshipQuerySet:
        return self.order_by(
            "subject_type",
            "subject_id",
            "optional_subject_relation",
            "resource_type",
            "resource_id",
            "relation",
            "caveat_name",
        )

    def wire_values(self) -> Any:
        return self.values(*WIRE_VALUE_FIELDS)


class RelationshipManager(models.Manager.from_queryset(RelationshipQuerySet)):  # type: ignore[misc]
    """Manager exposing the public relationship-query helper surface."""


class Relationship(models.Model):
    """Denormalized relationship row — historical default storage shape."""

    resource_type = models.CharField(max_length=64, db_index=True)
    resource_id = models.CharField(max_length=64, db_index=True)
    relation = models.CharField(max_length=64, db_index=True)
    subject_type = models.CharField(max_length=64, db_index=True)
    subject_id = models.CharField(max_length=64, db_index=True)
    optional_subject_relation = models.CharField(max_length=64, blank=True, default="")
    caveat_name = models.CharField(max_length=64, blank=True, default="")
    caveat_context = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid = models.BigIntegerField(default=0, db_index=True)

    objects = RelationshipManager()

    def save(self, *args: Any, **kwargs: Any) -> None:
        _owned_instance_save(
            self, lambda: super(Relationship, self).save(*args, **kwargs), kwargs.get("using")
        )

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        return _owned_instance_delete(
            self, lambda: super(Relationship, self).delete(*args, **kwargs), kwargs.get("using")
        )

    class Meta:
        app_label = "rebac"
        verbose_name = "Relationship"
        verbose_name_plural = "Relationships"
        indexes = [
            # Forward: "what subjects have <relation> on <resource>?"
            models.Index(
                fields=["resource_type", "resource_id", "relation"],
                name="rebac_rel_fwd_idx",
            ),
            # Reverse: "what resources does <subject> have <relation> on?"
            models.Index(
                fields=["subject_type", "subject_id", "relation"],
                name="rebac_rel_rev_idx",
            ),
            # Subject-set traversal (group#member -> user)
            models.Index(
                fields=["subject_type", "subject_id", "optional_subject_relation"],
                name="rebac_rel_subset_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "resource_type",
                    "resource_id",
                    "relation",
                    "subject_type",
                    "subject_id",
                    "optional_subject_relation",
                    "caveat_name",
                ],
                name="rebac_relationship_uniq",
            ),
        ]

    def __str__(self) -> str:
        return str(
            RelationshipTuple(
                resource=ObjectRef(self.resource_type, self.resource_id),
                relation=self.relation,
                subject=SubjectRef.of(
                    self.subject_type, self.subject_id, self.optional_subject_relation
                ),
                caveat_name=self.caveat_name,
            )
        )


def _translate_read_lookup(key: str) -> str:
    head, _, tail = key.partition("__")
    suffix = ("__" + tail) if tail else ""
    translated = _REGISTRY_WIRE_FIELD_MAP.get(head)
    if translated is None:
        return key
    return f"{translated}{suffix}"


def _wire_field_head(name: str) -> str | None:
    head = name.removeprefix("-").partition("__")[0]
    if head in _REGISTRY_WIRE_FIELD_MAP:
        return head
    return None


def _raise_for_wire_field_expression(value: Any, *, surface: str) -> None:
    if isinstance(value, F):
        field = _wire_field_head(getattr(value, "name", ""))
        if field is not None:
            raise RelationshipReadError(
                f"RelationshipRegistry cannot translate wire field {field} inside "
                f"{surface} expressions. Use for_resource(), for_subject(), "
                "wire_values(), order_by_resource(), order_by_subject(), or the "
                f"registry field {_REGISTRY_WIRE_FIELD_MAP[field]!r} explicitly."
            )
        return
    get_source_expressions = getattr(value, "get_source_expressions", None)
    if callable(get_source_expressions):
        for child in cast(Iterable[Any], get_source_expressions()):
            _raise_for_wire_field_expression(child, surface=surface)


def _translate_q_object(q_object: Q) -> Q:
    translated = q_object.copy()
    children: list[Any] = []
    for child in q_object.children:
        if isinstance(child, Q):
            children.append(_translate_q_object(child))
        elif isinstance(child, tuple) and len(child) == 2 and isinstance(child[0], str):
            key, value = child
            _raise_for_wire_field_expression(value, surface="filter()/exclude()")
            children.append((_translate_read_lookup(key), value))
        else:
            children.append(child)
    translated.children = children
    return translated


def _translate_read_args(args: tuple[Any, ...]) -> tuple[Any, ...]:
    translated: list[Any] = []
    for arg in args:
        if isinstance(arg, Q):
            translated.append(_translate_q_object(arg))
        else:
            _raise_for_wire_field_expression(arg, surface="filter()/exclude()")
            translated.append(arg)
    return tuple(translated)


def _translate_read_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Rewrite denormalized-style string lookups into FK-side lookups.

    The registry model stores ``(type, id)`` pairs in ``RebacResource`` and
    references them via ``resource_fk`` / ``subject_fk``. Engine code is
    written against the denormalized shape (where the strings are inline
    columns), so we translate at the manager boundary::

        resource_type="x" / resource_id="y" → resource_fk__resource_type / __resource_id
        subject_type__in=[...]              → subject_fk__resource_type__in=[...]

    Only the four denormalized field names ``resource_type`` /
    ``resource_id`` / ``subject_type`` / ``subject_id`` are rewritten; all
    other lookup kwargs pass through unchanged so the manager is a
    drop-in for the denormalized one.
    """
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        _raise_for_wire_field_expression(value, surface="filter()/exclude()/get()")
        out[_translate_read_lookup(key)] = value
    return out


def _translate_projection_fields(fields: tuple[Any, ...], *, surface: str) -> tuple[Any, ...]:
    translated: list[Any] = []
    for field in fields:
        if isinstance(field, str):
            translated.append(_translate_read_lookup(field))
        else:
            _raise_for_wire_field_expression(field, surface=surface)
            translated.append(field)
    return tuple(translated)


def _translate_ordering_fields(fields: tuple[Any, ...]) -> tuple[Any, ...]:
    translated: list[Any] = []
    for field in fields:
        if isinstance(field, str):
            if field == "?":
                translated.append(field)
                continue
            prefix = "-" if field.startswith("-") else ""
            raw = field[1:] if prefix else field
            translated.append(f"{prefix}{_translate_read_lookup(raw)}")
        else:
            _raise_for_wire_field_expression(field, surface="order_by()")
            translated.append(field)
    return tuple(translated)


class RelationshipRegistryQuerySet(models.QuerySet["RelationshipRegistry"]):
    """Translating QuerySet for :class:`RelationshipRegistry`.

    Rewriting the kwargs at the QuerySet level (rather than only on the
    manager) means chained calls like
    ``RelationshipRegistry.objects.filter(...).exclude(...)`` translate at
    every step — not just the first. The manager's ``get_queryset()``
    returns this class so the rewrite is in scope for the whole chain.
    """

    def delete(self) -> tuple[int, dict[str, int]]:
        return _owned_tuple_delete(self, super().delete)

    def update(self, **kwargs: Any) -> int:
        raise NotImplementedError(
            "Relationship queryset update() is unsupported; use delete_relationships() "
            "and write_relationships() to change tuples."
        )

    def index_projection(self) -> models.QuerySet[Any]:
        # Every F expression uses a real FK path. The internal projection is a
        # plain queryset: its wire aliases are now real annotations, and later
        # derivation F()/OuterRef() expressions must not hit the public wire
        # translator's intentional rejection of wire-field expressions.
        projected = self.annotate(
            **{name: F(path) for name, path in _REGISTRY_WIRE_FIELD_MAP.items()},
            subject_relation=F("optional_subject_relation"),
        )
        return models.QuerySet(
            model=self.model,
            query=projected.query,
            using=self.db,
            hints=getattr(self, "_hints", None),
        ).values(*INDEX_PROJECTION_FIELDS)

    def filter(self, *args: Any, **kwargs: Any) -> RelationshipRegistryQuerySet:
        return super().filter(*_translate_read_args(args), **_translate_read_kwargs(kwargs))

    def exclude(self, *args: Any, **kwargs: Any) -> RelationshipRegistryQuerySet:
        return super().exclude(*_translate_read_args(args), **_translate_read_kwargs(kwargs))

    def get(self, *args: Any, **kwargs: Any) -> Any:
        return super().get(*_translate_read_args(args), **_translate_read_kwargs(kwargs))

    def values(
        self, *fields: Any, **expressions: Any
    ) -> models.QuerySet[RelationshipRegistry, dict[str, Any]]:
        aliases: dict[str, Any] = {}
        translated_fields: list[Any] = []
        for field in fields:
            if isinstance(field, str) and field in _REGISTRY_WIRE_FIELD_MAP:
                aliases[field] = F(_REGISTRY_WIRE_FIELD_MAP[field])
            elif isinstance(field, str):
                translated_fields.append(_translate_read_lookup(field))
            else:
                _raise_for_wire_field_expression(field, surface="values()")
                translated_fields.append(field)
        for value in expressions.values():
            _raise_for_wire_field_expression(value, surface="values()")
        return super().values(*translated_fields, **{**aliases, **expressions})

    def values_list(
        self, *fields: Any, **kwargs: Any
    ) -> models.QuerySet[RelationshipRegistry, Any]:
        return super().values_list(
            *_translate_projection_fields(fields, surface="values_list()"),
            **kwargs,
        )

    def order_by(self, *field_names: Any) -> RelationshipRegistryQuerySet:
        return super().order_by(*_translate_ordering_fields(field_names))

    def annotate(self, *args: Any, **kwargs: Any) -> RelationshipRegistryQuerySet:
        for value in (*args, *kwargs.values()):
            _raise_for_wire_field_expression(value, surface="annotate()")
        return super().annotate(*args, **kwargs)

    def for_resource(self, resource_type: str, resource_id: str) -> RelationshipRegistryQuerySet:
        return self.filter(**{"resource_type": resource_type, "resource_id": resource_id})

    def for_subject(
        self,
        subject_type: str,
        subject_id: str,
        optional_relation: str | None = None,
    ) -> RelationshipRegistryQuerySet:
        qs = self.filter(**{"subject_type": subject_type, "subject_id": subject_id})
        if optional_relation is not None:
            qs = qs.filter(optional_subject_relation=optional_relation)
        return qs

    def order_by_resource(self) -> RelationshipRegistryQuerySet:
        return self.order_by(
            "resource_fk__resource_type",
            "resource_fk__resource_id",
            "relation",
            "subject_fk__resource_type",
            "subject_fk__resource_id",
            "optional_subject_relation",
            "caveat_name",
        )

    def order_by_subject(self) -> RelationshipRegistryQuerySet:
        return self.order_by(
            "subject_fk__resource_type",
            "subject_fk__resource_id",
            "optional_subject_relation",
            "resource_fk__resource_type",
            "resource_fk__resource_id",
            "relation",
            "caveat_name",
        )

    def wire_values(self) -> list[dict[str, Any]]:
        return [
            {
                "resource_type": row["resource_fk__resource_type"],
                "resource_id": row["resource_fk__resource_id"],
                "relation": row["relation"],
                "subject_type": row["subject_fk__resource_type"],
                "subject_id": row["subject_fk__resource_id"],
                "optional_subject_relation": row["optional_subject_relation"],
                "caveat_name": row["caveat_name"],
            }
            for row in self.values(
                "resource_fk__resource_type",
                "resource_fk__resource_id",
                "relation",
                "subject_fk__resource_type",
                "subject_fk__resource_id",
                "optional_subject_relation",
                "caveat_name",
            )
        ]


def _owned_tuple_delete(
    rows: RelationshipQuerySet | RelationshipRegistryQuerySet,
    delete: Callable[[], tuple[int, dict[str, int]]],
) -> tuple[int, dict[str, int]]:
    """Capture arbitrary queryset matches before their tuple rows disappear."""
    from ..backends.local import mark_relationships_changed
    from ..index.maintain import tuple_owner

    cast(Any, rows)._for_write = True
    if _engine_tuple_owner.get():
        return delete()
    with tuple_owner(rows.db) as maintenance:
        if maintenance is not None:
            projection = rows.index_projection()
            maintenance.capture_old(tuples=projected_tuples(projection))
        result = delete()
        if maintenance is not None:
            maintenance.changed()
    if result[0]:
        mark_relationships_changed()
    return result


class RelationshipRegistryManager(models.Manager.from_queryset(RelationshipRegistryQuerySet)):  # type: ignore[misc]
    """Translating manager for :class:`RelationshipRegistry`.

    Accepts the same ``(resource_type, resource_id, subject_type, subject_id)``
    string kwargs as :class:`Relationship` and upserts the corresponding
    :class:`RebacResource` rows transparently. The translation lives on
    the manager — engine code can keep building filter querysets the way
    it already does.

    Translation policy:

    - ``create()`` upserts both ``resource_fk`` and ``subject_fk`` and
      writes one row. Two extra SELECTs + one INSERT in the common path,
      all in one transaction.
    - ``get_or_create()`` / ``update_or_create()`` perform the same upsert
      then defer to the parent.
    - ``filter()`` / ``get()`` / ``exclude()`` translate string kwargs
      into FK-side lookups via :func:`_translate_read_kwargs`. Lookups
      for resources not in the registry collapse to "no match" — reads
      never create ``RebacResource`` rows.
    """

    def _translate_write_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Upsert ``RebacResource`` rows for any (type, id) pair in kwargs."""
        from .resource import RebacResource

        rt = kwargs.pop("resource_type", None)
        rid = kwargs.pop("resource_id", None)
        if rt is not None and rid is not None:
            kwargs["resource_fk"] = RebacResource.upsert_ref(rt, rid)
        elif rt is not None or rid is not None:
            raise ValueError(
                "RelationshipRegistry write needs both resource_type and resource_id "
                f"(got resource_type={rt!r}, resource_id={rid!r})"
            )
        st = kwargs.pop("subject_type", None)
        sid = kwargs.pop("subject_id", None)
        if st is not None and sid is not None:
            kwargs["subject_fk"] = RebacResource.upsert_ref(st, sid)
        elif st is not None or sid is not None:
            raise ValueError(
                "RelationshipRegistry write needs both subject_type and subject_id "
                f"(got subject_type={st!r}, subject_id={sid!r})"
            )
        return kwargs

    def get_queryset(self) -> RelationshipRegistryQuerySet:
        # Eager-join the FK rows. Engine code reads ``row.resource_type``
        # / ``row.subject_id`` per-row, so without select_related the
        # per-row property accessors would issue N queries.
        qs: RelationshipRegistryQuerySet = super().get_queryset()  # pyright: ignore[reportAssignmentType]
        return qs.select_related("resource_fk", "subject_fk")

    def create(self, **kwargs: Any) -> RelationshipRegistry:
        obj: RelationshipRegistry = super().create(**self._translate_write_kwargs(kwargs))
        return obj

    def get_or_create(
        self,
        defaults: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[RelationshipRegistry, bool]:
        kwargs = self._translate_write_kwargs(kwargs)
        result: tuple[RelationshipRegistry, bool] = super().get_or_create(
            defaults=defaults, **kwargs
        )
        return result

    def update_or_create(
        self,
        defaults: Mapping[str, Any] | None = None,
        create_defaults: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[RelationshipRegistry, bool]:
        kwargs = self._translate_write_kwargs(kwargs)
        result: tuple[RelationshipRegistry, bool] = super().update_or_create(
            defaults=defaults, create_defaults=create_defaults, **kwargs
        )
        return result


class RelationshipRegistry(models.Model):
    """Registry-mode relationship row.

    Same wire-shape as :class:`Relationship` but ``resource_*`` and
    ``subject_*`` collapse into two integer FKs pointing at
    :class:`rebac.models.RebacResource`. Reads of the denormalized string
    columns are proxied through ``resource_fk`` / ``subject_fk`` via the
    property accessors below.

    Writes via the public manager (``RelationshipRegistry.objects.create(
    resource_type='...', resource_id='...', ...)``) upsert the
    ``RebacResource`` rows transparently — callers see no shape change.
    """

    resource_fk = models.ForeignKey(
        "rebac.RebacResource",
        on_delete=models.CASCADE,
        related_name="+",
        db_column="resource_fk_id",
    )
    relation = models.CharField(max_length=64)
    subject_fk = models.ForeignKey(
        "rebac.RebacResource",
        on_delete=models.CASCADE,
        related_name="+",
        db_column="subject_fk_id",
    )
    optional_subject_relation = models.CharField(max_length=64, blank=True, default="")
    caveat_name = models.CharField(max_length=64, blank=True, default="")
    caveat_context = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    written_at_xid = models.BigIntegerField(default=0, db_index=True)

    objects = RelationshipRegistryManager()

    def save(self, *args: Any, **kwargs: Any) -> None:
        _owned_instance_save(
            self,
            lambda: super(RelationshipRegistry, self).save(*args, **kwargs),
            kwargs.get("using"),
        )

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        return _owned_instance_delete(
            self,
            lambda: super(RelationshipRegistry, self).delete(*args, **kwargs),
            kwargs.get("using"),
        )

    class Meta:
        app_label = "rebac"
        verbose_name = "Relationship (registry)"
        verbose_name_plural = "Relationships (registry)"
        indexes = [
            # Forward / reverse / subject-set parallels of the denormalized
            # indexes; integer-FK leading columns shrink the leaf-page
            # footprint ~5-10x for the hot lookup paths.
            models.Index(
                fields=["resource_fk", "relation"],
                name="rebac_reg_rel_fwd_idx",
            ),
            models.Index(
                fields=["subject_fk", "relation"],
                name="rebac_reg_rel_rev_idx",
            ),
            models.Index(
                fields=["subject_fk", "optional_subject_relation"],
                name="rebac_reg_rel_subset_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "resource_fk",
                    "relation",
                    "subject_fk",
                    "optional_subject_relation",
                    "caveat_name",
                ],
                name="rebac_relationship_reg_uniq",
            ),
        ]

    # Wire-shape accessors. Mirror :class:`Relationship`'s public attribute
    # surface so that engine code reading ``row.resource_type`` /
    # ``row.subject_id`` works in both modes without branching. The
    # accessors hit the joined FK row, so the manager's default queryset
    # eagerly ``select_related``s both to avoid N+1.
    @property
    def resource_type(self) -> str:
        return self.resource_fk.resource_type

    @property
    def resource_id(self) -> str:
        return self.resource_fk.resource_id

    @property
    def subject_type(self) -> str:
        return self.subject_fk.resource_type

    @property
    def subject_id(self) -> str:
        return self.subject_fk.resource_id

    def __str__(self) -> str:
        return str(
            RelationshipTuple(
                resource=ObjectRef(self.resource_type, self.resource_id),
                relation=self.relation,
                subject=SubjectRef.of(
                    self.subject_type, self.subject_id, self.optional_subject_relation
                ),
                caveat_name=self.caveat_name,
            )
        )


def active_relationship_model() -> type[Relationship] | type[RelationshipRegistry]:
    """Return the relationship model selected by ``REBAC_LOCAL_BACKEND_STORAGE``.

    Engine code (``LocalBackend``, ``rebac.relationships``,
    ``rebac.roles``-via-Relationship.objects) routes every read/write
    through this helper so the storage mode flip is a settings change, not
    a code change. External consumers that import ``Relationship`` or
    ``RelationshipRegistry`` by name keep working unchanged in their
    respective modes.

    The return type is a union of the two concrete model classes (not
    ``type[Model]``) so call sites can reach ``.objects`` without losing
    the manager type — both classes expose the same Django-default
    ``Manager`` plus, in the registry case, the extra translation
    helpers defined on ``RelationshipRegistryManager``.
    """
    if app_settings.REBAC_LOCAL_BACKEND_STORAGE == "registry":
        return RelationshipRegistry
    return Relationship
