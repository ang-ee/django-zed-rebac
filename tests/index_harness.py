"""Compare the actual index reader with source facts and two independent oracles."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import ExitStack
from unittest.mock import patch

from django.utils import timezone

from rebac._id import resource_id_attr
from rebac.backends.local import LocalBackend
from rebac.errors import PermissionDepthExceeded
from rebac.field_backing import (
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from rebac.resources import model_for_resource_type, model_resource_type, stores_rows
from rebac.schema.ast import ConstBinding
from rebac.types import ObjectRef, RelationshipTuple, SubjectRef
from tests.index_oracle import WalkerOracle
from tests.index_reference import ReferenceModel


def seed(tuples: Iterable[str], *, backend: LocalBackend | None = None) -> None:
    from rebac import backend as get_backend

    rows = []
    for wire in tuples:
        source, target = wire.split("@", 1)
        resource, relation = source.strip().rsplit("#", 1)
        rows.append(
            RelationshipTuple(ObjectRef.parse(resource), relation, SubjectRef.parse(target.strip()))
        )
    (backend or get_backend()).write_relationships(rows)


def _snapshot(backend: LocalBackend, *, using: str, extra_resources=()):
    """Read source facts, never IndexEdge: projection is itself under test."""
    from rebac.models import active_relationship_model

    schema = backend.schema()
    rows = [
        RelationshipTuple(
            ObjectRef(row["resource_type"], row["resource_id"]),
            row["relation"],
            SubjectRef.of(row["subject_type"], row["subject_id"], row["subject_relation"]),
            row["caveat_name"] or "",
            row["caveat_context"] or {},
            row["expires_at"],
        )
        for row in active_relationship_model().objects.using(using).index_projection()
    ]
    resources = {row.resource for row in rows} | set(extra_resources)
    resources.update(row.subject.object for row in rows)
    for definition in schema.definitions:
        model = model_for_resource_type(definition.resource_type)
        if model is not None and stores_rows(model):
            resources.update(
                ObjectRef(definition.resource_type, str(value))
                for value in model._base_manager.using(using).values_list(
                    resource_id_attr(model), flat=True
                )
            )
    for definition in schema.definitions:
        for relation in definition.relations:
            if relation.backing is None:
                continue
            field = resolve_field_backing(definition, relation)
            attribute = resolve_attribute_backing(definition, relation)
            const = resolve_const_backing(definition, relation)
            if attribute is not None:
                target_ids = attribute.target_model._base_manager.using(using).values_list(
                    attribute.target_id_attr, flat=True
                )
                resources.update(
                    ObjectRef(definition.resource_type, value)
                    for value in attribute.resource_ids_for_targets(target_ids, using=using)
                )
            rows = [
                row
                for row in rows
                if not (
                    row.resource.resource_type == definition.resource_type
                    and row.relation == relation.name
                    and relation.has_backing(row.resource.resource_id)
                )
            ]
            if isinstance(relation.backing, ConstBinding) and not relation.backing.filters:
                continue
            for resource in sorted(resources, key=str):
                if resource.resource_type != definition.resource_type:
                    continue
                if field is not None:
                    ids = field.queryset(resource_id=resource.resource_id, using=using).values_list(
                        field.target_values_path(), flat=True
                    )
                    target_type = field.target_resource_type
                elif attribute is not None and attribute.applies_to(resource.resource_id):
                    ids = attribute.subject_ids(resource.resource_id, using=using)
                    target_type = attribute.target_resource_type
                elif const is not None and const.matches(resource.resource_id, using=using):
                    ids = [const.target_id]
                    target_type = const.target_resource_type
                else:
                    continue
                rows.extend(
                    RelationshipTuple(
                        resource,
                        relation.name,
                        SubjectRef.of(
                            target_type, str(value), relation.allowed_subjects[0].relation
                        ),
                    )
                    for value in ids
                    if value is not None
                )
    return schema, rows, resources


def assert_index_matches(
    *, subjects, resources, actions, contexts=(None,), now=None, using="default"
) -> None:
    from rebac import backend as get_backend
    from rebac.index import read

    active = get_backend()
    assert isinstance(active, LocalBackend)
    resources, subjects, actions, contexts = map(tuple, (resources, subjects, actions, contexts))
    now = now or timezone.now()
    with ExitStack() as stack:
        stack.enter_context(patch("django.utils.timezone.now", return_value=now))
        schema, tuples, universe = _snapshot(active, using=using, extra_resources=resources)
        reference = ReferenceModel(schema, tuples, now=now)
        reference.resources.update(universe)
        oracle = WalkerOracle()
        stack.enter_context(patch.object(oracle, "schema", return_value=schema))
        for resource in resources:
            for subject in subjects:
                for action in actions:
                    for context in contexts:
                        expected = reference.check(
                            subject=subject, action=action, resource=resource, context=context
                        )
                        try:
                            walked = oracle.check_access(
                                subject=subject,
                                action=action,
                                resource=resource,
                                context=context,
                                using=using,
                            )
                        except PermissionDepthExceeded:
                            # Positive data cycles and paths beyond the frozen
                            # walker's bound use the finite-path denotation.
                            # The index/reference assertion below still runs.
                            walked = expected
                        actual = read.check(
                            resource=resource,
                            action=action,
                            actor=subject,
                            context=context,
                            using=using,
                        )
                        details = (resource, subject, action, context)
                        assert (actual.result, actual.conditional_on) == (
                            expected.result,
                            expected.conditional_on,
                        ), (details, actual, expected)
                        # The walker's missing parameters depend on the order
                        # of arms and rows, and include those of paths that
                        # fail. The index reports the ones the result needs.
                        assert actual.result == walked.result, (details, actual, walked)
                        assert set(actual.conditional_on) <= set(walked.conditional_on), (
                            details,
                            actual,
                            walked,
                        )


def assert_subjects_match(*, resources, actions, subject_types, now=None, using="default") -> None:
    """Compare subject enumeration with the reference; the walker's is incomplete (D4)."""
    from rebac import backend as get_backend
    from rebac.index import read

    active = get_backend()
    assert isinstance(active, LocalBackend)
    resources = tuple(resources)
    now = now or timezone.now()
    with patch("django.utils.timezone.now", return_value=now):
        schema, tuples, universe = _snapshot(active, using=using, extra_resources=resources)
        reference = ReferenceModel(schema, tuples, now=now)
        reference.resources.update(universe)
        for resource in resources:
            for action in actions:
                for subject_type in subject_types:
                    expected = reference.lookup_subjects(
                        resource=resource, action=action, subject_type=subject_type
                    )
                    actual = read.lookup_subjects(
                        resource=resource, action=action, subject_type=subject_type, using=using
                    )
                    assert set(actual) == expected, (resource, action, subject_type)


def assert_scope_matches(model, *, actor: SubjectRef, action: str) -> None:
    from rebac.index import read

    query = model._base_manager.all()
    using = query.db
    resource_type = model_resource_type(model)
    assert resource_type is not None
    resources = tuple(
        ObjectRef(resource_type, str(value))
        for value in query.values_list(resource_id_attr(model), flat=True)
    )
    assert_index_matches(subjects=(actor,), resources=resources, actions=(action,), using=using)
    expected = {
        obj.pk
        for obj in query
        if read.check(
            resource=ObjectRef(resource_type, str(getattr(obj, resource_id_attr(model)))),
            action=action,
            actor=actor,
            context=None,
            using=using,
        ).allowed
    }
    actual = set(
        model._base_manager.using(using)
        .filter(read.scope_q(model, action=action, actor=actor, using=using))
        .values_list("pk", flat=True)
    )
    assert actual == expected


def assert_no_drift(*, using: str = "default") -> None:
    from rebac.index.rebuild import verify

    drift = verify(using=using)
    assert drift == [], drift
