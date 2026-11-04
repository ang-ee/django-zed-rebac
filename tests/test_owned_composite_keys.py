import pytest
from django.db import connection, models
from django.test.utils import isolate_apps

import rebac.watch as watch
from rebac import SubjectRef, actor_context
from rebac.mixins import RebacTrackedMixin
from rebac.testing import install_schema

SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation viewer: auth/user
    permission read = viewer
}
"""


@isolate_apps("tests.testapp")
@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_watched_composite_pk_tracked_queryset_delete(monkeypatch):
    class PairRow(RebacTrackedMixin):
        a = models.IntegerField()
        b = models.IntegerField()
        pk = models.CompositePrimaryKey("a", "b")

        class Meta:
            app_label = "testapp"

    install_schema(SCHEMA)
    real = watch.model_is_watched
    monkeypatch.setattr(
        watch,
        "model_is_watched",
        lambda watched, model, names=None: (
            True if model is PairRow else real(watched, model, names)
        ),
    )
    with connection.schema_editor() as editor:
        editor.create_model(PairRow)
    try:
        PairRow._base_manager.bulk_create([PairRow(a=1, b=2), PairRow(a=3, b=4)])
        captured = PairRow._base_manager.filter(watch.key_filter(PairRow, {(1, 2), (3, 4)}))
        assert set(captured.values_list("pk", flat=True)) == {(1, 2), (3, 4)}
        with actor_context(SubjectRef.of("auth/user", "x")):
            PairRow._base_manager.filter(a__in=[1, 3]).delete()
        assert not PairRow._base_manager.exists()
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(PairRow)
