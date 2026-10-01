"""Production-shaped intersections: a scope agrees with point checks and is fixed by the policy."""

from contextlib import contextmanager

import pytest
from django.db import connection, models
from django.test import override_settings
from django.test.utils import isolate_apps

from rebac import (
    PermissionDepthExceeded,
    RebacMixin,
    RelationshipTuple,
    SubjectRef,
    backend,
    sudo,
    to_object_ref,
)
from rebac.backends import reset_backend
from rebac.conf import app_settings
from rebac.schema import parse_zed
from tests.backend_setup import STORAGE_TIERS, install_schema

pytestmark = pytest.mark.django_db(transaction=True)


@contextmanager
def binding_graph(db, monkeypatch, storage):
    """The same graph can run on SQLite and the opt-in vendor connections."""
    import rebac.compile.predicate
    import rebac.compile.read
    import rebac.field_backing
    import rebac.resources
    import rebac.watch

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
        # Isolated classes are absent from the process-global app registry
        # that resolves a resource or subject type to its model.
        original = rebac.field_backing.model_for_resource_type
        for module in (
            rebac.field_backing,
            rebac.resources,
            rebac.watch,
            rebac.compile.predicate,
            rebac.compile.read,
        ):
            monkeypatch.setattr(
                module, "model_for_resource_type", lambda name: types.get(name) or original(name)
            )
        original_subject = rebac.field_backing.model_for_subject_type
        for module in (
            rebac.field_backing,
            rebac.watch,
            rebac.compile.predicate,
            rebac.compile.read,
        ):
            monkeypatch.setattr(
                module,
                "model_for_subject_type",
                lambda name: (types[name], "pk") if name in types else original_subject(name),
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
        # The write gates must resolve the watched columns to these isolated classes.
        watched = rebac.watch.gate_policy(db.alias, active).watched
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
        actions = ("left", "right", "read", "exclude", "shared")

        def create(name, **fields):
            with sudo(reason="additive scope fixture"):
                return types[f"scope/{name}"]._base_manager.using(db.alias).create(**fields)

        root = create("page")
        pages = [root]
        for _ in range(12):
            pages.append(create("page", parent=pages[-1]))
        # A page inherits from REBAC_DEPTH_LIMIT ancestors at most: the root's
        # grant still reaches ``near``, and ``far`` lies past the bound.
        near, far = pages[app_settings.REBAC_DEPTH_LIMIT], pages[-1]
        vault = create("vault")
        target = create("stage5")
        leaf = target
        for name in [*(f"stage{i}" for i in reversed(range(5))), "project", "task"]:
            target = create(name, parent=target)
        task = target
        rows = [
            create("binding", page=near, task=task),
            create("binding", vault=vault, project=task.parent),
            create("binding", page=near),
            create("binding", task=task),
            create("binding"),
        ]
        beyond = [create("binding", page=far, task=task), create("binding", page=far)]
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
        undecided = {action: set() for action in actions}
        for actor in actors:
            for action in actions:
                qs = binding.objects.using(db.alias).with_actor(actor).with_action(action).scoped()
                probes = []

                def record(execute, sql, params, many, context, probes=probes):
                    probes.append((sql, params))
                    return execute(sql, params, many, context)

                with db.execute_wrapper(record):
                    sql, params = qs.query.get_compiler(using=db.alias).as_sql()
                # Compilation decides the actor's sets and the small row sets
                # behind arrows; it reads, and writes nothing.
                assert all(probe_sql.lstrip().startswith("SELECT") for probe_sql, _ in probes)
                sizes[action] = (len(sql), len(params))
                expected = set()
                for position, row in enumerate([*rows, *beyond]):
                    try:
                        allowed = active.check_access(
                            subject=actor, action=action, resource=to_object_ref(row)
                        ).allowed
                    except PermissionDepthExceeded:
                        # The page's grant, if any, lies past the bound: no point answer.
                        assert row in beyond
                        undecided[action].add((actor.subject_id, position - len(rows)))
                    else:
                        if allowed:
                            expected.add(row.pk)
                # A scope keeps only rows provable within the bound.
                assert set(qs.values_list("pk", flat=True)) == expected
                if action == "left":
                    held = {rows[0].pk, rows[2].pk} if actor in actors[:2] else set()
                    assert expected == held | ({rows[1].pk} if actor == actors[0] else set())
                if action == "read":
                    assert expected == ({rows[0].pk, rows[1].pk} if actor == actors[0] else set())
        # Past the bound a point check raises exactly where the unreached
        # ancestors of the far page would decide the answer.
        everyone = {actor.subject_id for actor in actors}
        assert undecided == {
            "left": {(name, row) for name in everyone for row in (0, 1)},
            "right": set(),
            "read": {("both", 0), ("right", 0)},
            "exclude": {("left", 0), ("neither", 0)} | {(name, 1) for name in everyone},
            "shared": {("both", 0), ("right", 0)},
        }
        # A compound statement holds its operands side by side: an intersection
        # or an exclusion of recursive unions does not multiply them.
        operands = {
            "read": ("left", "right"),
            "exclude": ("left", "right"),
            # ``project->read`` is an arm of ``right``.
            "shared": ("left", "right", "right"),
        }
        for action, parts in operands.items():
            length, parameters = sizes[action]
            assert length <= sum(sizes[part][0] for part in parts)
            assert parameters <= sum(sizes[part][1] for part in parts)


# Intrinsically slow: an eight-model production-shaped graph checked row by row.
@pytest.mark.slow
@pytest.mark.parametrize("storage", ["denormalized", "registry"])
def test_recursive_intersection_is_additive(monkeypatch, storage):
    exercise_binding_scope(connection, monkeypatch, storage)


@pytest.mark.parametrize("storage", STORAGE_TIERS)
@pytest.mark.parametrize("deep", [3, pytest.param(12, marks=pytest.mark.slow)])
def test_scope_sql_is_independent_of_recursive_depth(monkeypatch, storage, deep):
    with binding_graph(connection, monkeypatch, storage) as (_active, types, binding):
        actor = SubjectRef.of("auth/user", "both")
        shapes = []
        parent = None
        for depth in (1, deep):
            with sudo(reason="scope depth fixture"):
                for _ in range(depth):
                    parent = types["scope/page"].objects.create(parent=parent)
            qs = binding.objects.with_actor(actor).scoped()
            sql, params = qs.query.sql_with_params()
            shapes.append((sql, len(params)))
            assert qs.count() == 0
        assert shapes[0] == shapes[1]
