"""Production-shaped intersections have SQL bounded by their read plans."""

from contextlib import contextmanager

import pytest
from django.db import connection, models
from django.test import override_settings
from django.test.utils import isolate_apps

from rebac import RebacMixin, RelationshipTuple, SubjectRef, backend, sudo, to_object_ref
from rebac.backends import reset_backend
from rebac.index.program import program_for
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.index_harness import assert_no_drift

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def binding_graph(db, monkeypatch, storage):
    """The same graph can run on SQLite and the opt-in vendor connections."""
    import rebac.field_backing
    import rebac.index.program
    import rebac.index.read
    import rebac.resources

    with (
        isolate_apps("tests.testapp"),
        override_settings(REBAC_LOCAL_BACKEND_STORAGE=storage),
    ):
        types = {}

        def model(name, **fields):
            cls = type(
                name.title(),
                (RebacMixin, models.Model),
                {
                    "__module__": __name__,
                    "Meta": type(
                        "Meta", (), {"app_label": "testapp", "rebac_resource_type": f"scope/{name}"}
                    ),
                    **fields,
                },
            )
            types[f"scope/{name}"] = cls
            return cls

        def fk(target):
            return models.ForeignKey(target, null=True, on_delete=models.CASCADE, related_name="+")

        page = model("page", parent=fk("self"))
        vault = model("vault")
        target = None
        for name in [*(f"stage{i}" for i in reversed(range(6))), "project", "task"]:
            target = model(name, **({"parent": fk(target)} if target else {}))
        binding = model(
            "binding",
            page=fk(page),
            vault=fk(vault),
            task=fk(target),
            project=fk(types["scope/project"]),
        )
        original = rebac.field_backing.model_for_resource_type
        monkeypatch.setattr(
            rebac.field_backing,
            "model_for_resource_type",
            lambda name: types.get(name) or original(name),
        )
        for module in (rebac.resources, rebac.index.program, rebac.index.read):
            monkeypatch.setattr(
                module, "model_for_resource_type", lambda name: types.get(name) or original(name)
            )
        original_subject = rebac.field_backing.model_for_subject_type
        monkeypatch.setattr(
            rebac.field_backing,
            "model_for_subject_type",
            lambda name: (types[name], "pk") if name in types else original_subject(name),
        )
        monkeypatch.setattr(
            rebac.index.program,
            "model_for_subject_type",
            rebac.field_backing.model_for_subject_type,
        )
        schema = """
        definition auth/user {}
        definition scope/group { relation member: auth/user }
        definition scope/role { relation member: auth/user | scope/group#member }
        definition scope/page {
            relation viewer: auth/user | scope/role#member
            relation parent: scope/page // rebac:field=parent
            permission read = viewer + parent->read
        }
        definition scope/vault {
            relation viewer: auth/user | scope/role#member
            permission read = viewer
        }
        definition scope/binding {
            relation page: scope/page // rebac:field=page
            relation vault: scope/vault // rebac:field=vault
            relation task: scope/task // rebac:field=task
            relation project: scope/project // rebac:field=project
            permission left = page->read + vault->read
            permission right = task->read + project->read
            permission read = left & right
            permission exclude = left - right
            permission shared = (left + project->read) & right
        }
        """
        for name in ["task", "project", *(f"stage{i}" for i in range(6))]:
            names = ["task", "project", *(f"stage{i}" for i in range(6))]
            index = names.index(name)
            parent = names[index + 1] if index < len(names) - 1 else None
            relation = f"relation parent: scope/{parent} // rebac:field=parent\n" if parent else ""
            expr = "viewer + parent->read" if parent else "viewer"
            schema += f"definition scope/{name} {{\nrelation viewer: auth/user | scope/role#member\n{relation}permission read = {expr}\n}}\n"
        with db.schema_editor() as editor:
            for cls in types.values():
                editor.create_model(cls)
        reset_backend()
        active = backend()
        install_schema(active, parse_zed(schema))
        # Watch publication must retain these isolated classes, which cannot
        # be resolved by the process-global Django app registry.
        from rebac.index.program import program_for

        watched = program_for(active, using=db.alias).watched
        assert watched[binding._meta.label_lower].model is binding
        try:
            yield active, types, binding
        finally:
            reset_backend()
            with db.schema_editor() as editor:
                for cls in reversed(types.values()):
                    editor.delete_model(cls)


def exercise_binding_scope(db, monkeypatch, storage):
    with binding_graph(db, monkeypatch, storage) as (active, types, binding):
        actors = [SubjectRef.of("auth/user", name) for name in ("both", "left", "right", "neither")]

        def create(name, **fields):
            with sudo(reason="additive scope fixture"):
                return types[f"scope/{name}"]._base_manager.using(db.alias).create(**fields)

        root = create("page")
        page = root
        for _ in range(12):
            page = create("page", parent=page)
        vault = create("vault")
        target = create("stage5")
        leaf = target
        for name in [*(f"stage{i}" for i in reversed(range(5))), "project", "task"]:
            target = create(name, parent=target)
        task = target
        rows = [
            create("binding", page=page, task=task),
            create("binding", vault=vault, project=task.parent),
            create("binding", page=page),
            create("binding", task=task),
            create("binding"),
        ]
        tuples = []
        for name, resource, members in (
            ("left", root, actors[:2]),
            ("right", leaf, [actors[0], actors[2]]),
        ):
            role = SubjectRef.of("scope/role", name, "member")
            group = SubjectRef.of("scope/group", name, "member")
            tuples.extend(
                [
                    RelationshipTuple(to_object_ref(resource), "viewer", role),
                    RelationshipTuple(role.object, "member", group),
                    *(RelationshipTuple(group.object, "member", actor) for actor in members),
                ]
            )
        tuples.append(RelationshipTuple(to_object_ref(vault), "viewer", actors[0]))
        active.write_relationships(tuples)

        sizes = {}
        for actor in actors:
            for action in ("left", "right", "read", "exclude", "shared"):
                qs = binding.objects.using(db.alias).with_actor(actor).with_action(action).scoped()
                probes = []

                def record(execute, sql, params, many, context, probes=probes):
                    probes.append((sql, params))
                    return execute(sql, params, many, context)

                with db.execute_wrapper(record):
                    sql, params = qs.query.get_compiler(using=db.alias).as_sql()
                assert probes == []  # SQL compilation must perform no graph reads.
                sizes[action] = (len(sql), len(params))
                expected = {
                    row.pk
                    for row in rows
                    if active.check_access(
                        subject=actor, action=action, resource=to_object_ref(row)
                    ).allowed
                }
                assert set(qs.values_list("pk", flat=True)) == expected
                if action == "read":
                    assert expected == ({rows[0].pk, rows[1].pk} if actor == actors[0] else set())
        plan = program_for(active, using=db.alias)
        for action, (length, parameters) in sizes.items():
            lookups = plan.lookups(("scope/binding", action))
            assert length <= 1024 + 4096 * lookups
            assert parameters <= 128 * lookups
        assert_no_drift(using=db.alias)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_recursive_intersection_is_additive(monkeypatch, storage):
    exercise_binding_scope(connection, monkeypatch, storage)


@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_scope_sql_is_independent_of_recursive_depth(monkeypatch, storage):
    with binding_graph(connection, monkeypatch, storage) as (_active, types, binding):
        actor = SubjectRef.of("auth/user", "both")
        shapes = []
        parent = None
        for depth in (1, 12):
            with sudo(reason="scope depth fixture"):
                for _ in range(depth):
                    parent = types["scope/page"].objects.create(parent=parent)
            qs = binding.objects.with_actor(actor).scoped()
            sql, params = qs.query.sql_with_params()
            shapes.append((sql, len(params)))
            assert qs.count() == 0
        assert shapes[0] == shapes[1]
