"""Recursive path CTEs ignore a resource model's default ordering."""

import pytest
from django.db import connection, models
from django.db.models import Exists, OuterRef
from django.test.utils import CaptureQueriesContext, isolate_apps

import rebac.resources
from rebac import RebacMixin, RelationshipTuple, SubjectRef, decision_scope, sudo, to_object_ref
from rebac.testing import install_schema

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.pg_delta]


@pytest.fixture
def ordered_path(monkeypatch):
    with isolate_apps("tests.testapp"):

        class OrderedPathHop(models.Model):
            name = models.CharField(max_length=10, default="hop")

            class Meta:
                app_label = "testapp"
                ordering = ("name",)

        class OrderedPathNode(RebacMixin, models.Model):
            status = models.CharField(max_length=10)
            hop = models.ForeignKey(
                OrderedPathHop, null=True, on_delete=models.SET_NULL, related_name="nodes"
            )

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/orderedpathnode"
                ordering = ("status", "-id")

        OrderedPathHop.add_to_class(
            "up",
            models.ForeignKey(
                OrderedPathNode, null=True, on_delete=models.SET_NULL, related_name="hops_up"
            ),
        )
        original_types = rebac.resources._model_types
        monkeypatch.setattr(
            rebac.resources,
            "_model_types",
            lambda prefix: (
                *original_types(prefix),
                (f"{prefix}test/orderedpathnode", OrderedPathNode),
            ),
        )
        with connection.schema_editor() as editor:
            editor.create_model(OrderedPathNode)
            editor.create_model(OrderedPathHop)
        try:
            local = install_schema("""
                definition auth/user {}
                definition test/orderedpathnode {
                    relation owner: auth/user
                    relation parent: test/orderedpathnode // rebac:field=hop__up
                    permission chain = owner + parent->chain
                }
            """)
            actor = SubjectRef.of("auth/user", "ordered-path")
            with sudo(reason="ordered recursive path fixture"):
                seed = OrderedPathNode.objects.create(status="z")
                child = OrderedPathNode.objects.create(status="a")
                grandchild = OrderedPathNode.objects.create(status="m")
                orphan = OrderedPathNode.objects.create(status="b")
                for node, parent in ((child, seed), (grandchild, child)):
                    node.hop = OrderedPathHop.objects.create(name=f"hop-{node.pk}", up=parent)
                    node.save()
            local.write_relationships([RelationshipTuple(to_object_ref(seed), "owner", actor)])
            nodes = (seed, child, grandchild, orphan)
            yield local, actor, OrderedPathNode, nodes
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(OrderedPathHop)
                editor.delete_model(OrderedPathNode)


@pytest.mark.parametrize("shape", ["list", "pk_in", "exists", "decision_scope", "handle"])
def test_recursive_path_on_ordered_resource_matches_point_checks(ordered_path, shape):
    local, actor, model, nodes = ordered_path
    truth = {
        node.pk
        for node in nodes
        if local.check_access(subject=actor, action="chain", resource=to_object_ref(node)).allowed
    }
    assert truth == {node.pk for node in nodes[:3]}
    candidates = [nodes[1].pk, nodes[2].pk, nodes[3].pk]
    with CaptureQueriesContext(connection) as captured:
        if shape == "list":
            rows = list(
                model.objects.with_actor(actor).with_action("chain").values_list("pk", flat=True)
            )
            assert rows == [nodes[1].pk, nodes[2].pk, nodes[0].pk]
        elif shape == "pk_in":
            rows = list(
                model.objects.with_actor(actor)
                .with_action("chain")
                .filter(pk__in=candidates)
                .values_list("pk", flat=True)
            )
            assert set(rows) == truth & set(candidates)
        elif shape == "exists":
            scoped = model.objects.with_actor(actor).with_action("chain").scoped_for_aggregate()
            rows = list(
                model._base_manager.annotate(
                    admitted=Exists(scoped.filter(pk=OuterRef("pk")))
                ).values_list("pk", "admitted")
            )
            assert dict(rows) == {node.pk: node.pk in truth for node in nodes}
        elif shape == "decision_scope":
            with decision_scope():
                rows = list(
                    model.objects.with_actor(actor)
                    .with_action("chain")
                    .filter(pk__in=candidates)
                    .values_list("pk", flat=True)
                )
            assert set(rows) == truth & set(candidates)
        else:
            handle = model.objects.with_actor(actor).with_action("chain").scope_cte()
            rows = list(
                handle.attach(model.objects.with_actor(actor).with_action("chain")).values_list(
                    "pk", flat=True
                )
            )
            assert set(rows) == truth
    path_sql = [query["sql"] for query in captured.captured_queries if "rebac_path" in query["sql"]]
    assert path_sql
    assert path_sql[-1].count("ORDER BY") == 1
    assert path_sql[-1].endswith(
        'ORDER BY "testapp_orderedpathnode"."status" ASC, "testapp_orderedpathnode"."id" DESC'
    )
