"""Compile-time frontier probes preserve depth errors and live scopes."""

from contextlib import nullcontext

import pytest
from django.db import connection
from django.db.models import Count, Exists, F, OuterRef
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from rebac import PermissionDepthExceeded, evaluator_scope
from tests.test_recursive_queryscope import ACTOR, OUTSIDER, chain, grant, schema_context
from tests.testapp.models import Folder

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
@pytest.mark.parametrize("reuse", [False, True])
def test_depth_probe_runs_at_each_compilation(storage, reuse):
    with schema_context(storage, "folder", "field") as (active, member, hop, _):
        rows = chain(active, hop, "field", 2)
        grant(active, rows[0], member)
        with evaluator_scope() if reuse else nullcontext():
            qs = Folder.objects.with_actor(ACTOR).scoped()
            with CaptureQueriesContext(connection) as captured:
                assert str(qs.query)
                assert len(captured) == 1
                assert qs.query.get_compiler(using="default").as_sql()
            assert len(captured) == 2
            operations = [
                lambda: qs.count(),
                lambda: qs.exists(),
                lambda: list(qs.iterator()),
                lambda: qs[:2].aggregate(n=Count("pk")),
                lambda: Folder._base_manager.filter(pk__in=qs.values("pk")).count(),
                lambda: Folder._base_manager.filter(Exists(qs.filter(pk=OuterRef("pk")))).count(),
            ]
            for operation in operations:
                with CaptureQueriesContext(connection) as captured:
                    assert operation()
                assert len(captured) == 2  # Frontier and scoped read, even with plan reuse.


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_rendering_and_execution_both_raise_on_overflow(storage):
    with (
        override_settings(REBAC_DEPTH_LIMIT=1),
        schema_context(storage, "folder", "field") as (active, _, hop, _),
    ):
        rows = chain(active, hop, "field", 2)
        qs = Folder.objects.with_actor(OUTSIDER).filter(pk=rows[-1].pk).scoped()
        for operation in (
            lambda: str(qs.query),
            lambda: qs.query.get_compiler(using="default").as_sql(),
            qs.exists,
        ):
            with pytest.raises(PermissionDepthExceeded, match="Depth limit 1 exceeded"):
                operation()


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_only_compiled_annotations_probe_their_frontier(storage):
    with (
        override_settings(REBAC_DEPTH_LIMIT=1),
        schema_context(storage, "folder", "field") as (active, _, hop, _),
    ):
        rows = chain(active, hop, "field", 2)
        overflow = Folder.objects.with_actor(OUTSIDER).filter(pk=rows[-1].pk)
        qs = Folder._base_manager.alias(overflow=Exists(overflow))
        assert qs.count() == 3  # An unused alias is absent from the executed SQL.
        assert qs.values("pk").distinct().count() == 3
        for ordering in ("overflow", F("overflow").asc()):
            with pytest.raises(PermissionDepthExceeded, match="Depth limit 1 exceeded"):
                list(qs.order_by(ordering))
