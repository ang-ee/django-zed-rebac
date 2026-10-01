"""Filtered constants share one row predicate across all authorization surfaces."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.db.models import Value

from rebac import (
    ObjectRef,
    PermissionDenied,
    SchemaError,
    SubjectRef,
    check_new,
    evaluator_scope,
    sudo,
)
from rebac.backends import backend as active_backend
from rebac.backends import reset_backend
from rebac.checks import check_field_backed_relations
from rebac.models import Relationship, SchemaDefinition, SchemaPermission, SchemaRelation
from rebac.preflight import _check_new_model
from rebac.schema import ConstBinding, ParseError, parse_zed, render_zed
from rebac.schema.ast import backing_from_dict, backing_to_dict
from rebac.schema.introspection import live_backed_resource_types
from tests.backend_setup import install_schema, rebuild_backend

from .testapp.models import Folder, Post

SCHEMA = """
definition auth/user {}
definition site/audience { permission read = authenticated }
definition blog/folder {
    relation public: site/audience // rebac:const={"target_id":"public","filters":{"is_active":true}}
    relation all_rows: site/audience // rebac:const=public
    permission read = public->read
    permission inverse = authenticated - public->read
    permission create = public->read
    permission unrestricted = all_rows->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""
ACTOR = SubjectRef.of("auth/user", "reader")


@pytest.fixture
def backend(db):
    reset_backend()
    result = active_backend()
    install_schema(result, parse_zed(SCHEMA))
    yield result
    reset_backend()


def ref(row):
    return ObjectRef(row._meta.rebac_resource_type, str(row.pk))


def test_constant_codec_render_and_legacy_bytes():
    schema = parse_zed(SCHEMA)
    binding = schema.get_definition("blog/folder").relations[0].backing
    assert binding == ConstBinding("public", (("is_active", True),))
    assert backing_from_dict(backing_to_dict(binding)) == binding
    rendered = render_zed(schema)
    assert render_zed(parse_zed(rendered)) == rendered
    assert '// rebac:const={"filters":{"is_active":true},"target_id":"public"}' in rendered
    assert "rebac:const=public\n" in rendered
    assert "rebac:const" not in render_zed(schema, include_backing=False)
    old = ConstBinding("public")
    assert backing_to_dict(old) == {"kind": "const", "target_id": "public"}
    empty = SCHEMA.replace("const=public", 'const={"target_id":"public","filters":{}}')
    assert render_zed(parse_zed(empty)) == rendered
    # Public introspection retains its 0.18.2 schema-only classification.
    assert live_backed_resource_types(schema) == frozenset({"blog/post"})
    legacy = (
        "definition blog/folder {\n    relation public: site/audience // rebac:const=public\n}\n"
    )
    assert render_zed(parse_zed(legacy)) == legacy


@pytest.mark.parametrize(
    "body",
    [
        {"filters": {}},
        {"target_id": "*"},
        {"target_id": 1},
        {"target_id": "public", "unknown": True},
        {"target_id": "public", "kind": "const"},
        {"target_id": "public", "filters": []},
        {"target_id": "public", "filters": {"name__in": ["a", "b"]}},
        {"target_id": "public", "filters": {"name": {"nested": True}}},
        {"target_id": "public", "filters": {"name": float("inf")}},
    ],
)
def test_filtered_constant_rejects_invalid_scalar_grammar(body):
    with pytest.raises(ParseError):
        parse_zed(
            "definition blog/folder {\n relation public: site/audience // rebac:const="
            + json.dumps(body)
            + "\n}"
        )


def test_filtered_constant_check_scope_exclusion_and_arrows(backend):
    with sudo(reason="filtered-constant fixtures"):
        matching = Folder.objects.create(name="public")
        hidden = Folder.objects.create(name="private", is_active=False)
        visible_post = Post.objects.create(title="visible", folder=matching)
        Post.objects.create(title="hidden", folder=hidden)
    for folder, allowed in [(matching, True), (hidden, False)]:
        assert (
            backend.check_access(subject=ACTOR, action="read", resource=ref(folder)).allowed
            is allowed
        )
        assert (
            backend.check_access(subject=ACTOR, action="inverse", resource=ref(folder)).allowed
            is not allowed
        )
        assert (
            backend.check_access(
                subject=SubjectRef.of("site/audience", "public"),
                action="public",
                resource=ref(folder),
            ).allowed
            is allowed
        )
        assert list(
            backend.lookup_subjects(
                resource=ref(folder), action="public", subject_type="site/audience"
            )
        ) == ([SubjectRef.of("site/audience", "public")] if allowed else [])
    assert not backend.grants_all(subject=ACTOR, action="read", resource_type="blog/folder")
    assert backend.grants_all(subject=ACTOR, action="unrestricted", resource_type="blog/folder")
    assert set(backend.accessible(subject=ACTOR, action="read", resource_type="blog/folder")) == {
        str(matching.pk)
    }
    assert set(
        backend.accessible(
            subject=SubjectRef.of("site/audience", "public"),
            action="public",
            resource_type="blog/folder",
        )
    ) == {str(matching.pk)}
    with patch.object(backend, "accessible", side_effect=AssertionError("must compile to SQL")):
        assert list(Folder.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [matching.pk]
        assert list(
            Folder.objects.with_actor(ACTOR).with_action("inverse").values_list("pk", flat=True)
        ) == [hidden.pk]
        assert list(Post.objects.with_actor(ACTOR).values_list("pk", flat=True)) == [
            visible_post.pk
        ]
    assert not Relationship.objects.exists()
    assert not backend.has_access(
        subject=ACTOR, action="read", resource=ObjectRef("blog/folder", "9999")
    )


@pytest.mark.django_db(transaction=True)
def test_column_changes_are_live_in_same_evaluator_scope(backend):
    with sudo(reason="filtered-constant fixture"):
        folder = Folder.objects.create(name="public")
    with evaluator_scope() as evaluator:
        for active in (True, False, True):
            Folder.objects.sudo(reason="backing fixture update").filter(pk=folder.pk).update(
                is_active=active
            )
            assert (
                evaluator.check(backend, subject=ACTOR, action="read", resource=ref(folder)).allowed
                is active
            )
            assert evaluator.accessible(
                backend, subject=ACTOR, action="read", resource_type="blog/folder"
            ) == ((str(folder.pk),) if active else ())
            assert Folder.objects.with_actor(ACTOR).filter(pk=folder.pk).exists() is active
    assert not Relationship.objects.exists()


@pytest.mark.parametrize("method", ["save", "create", "insert", "bulk_create"])
@pytest.mark.parametrize("active", [False, True])
def test_create_paths_evaluate_candidate_defaults_and_filters(backend, method, active):
    candidate = Folder(name="candidate", **({} if active else {"is_active": False}))
    manager = Folder.objects.with_actor(ACTOR)

    def persist():
        if method == "save":
            candidate.with_actor(ACTOR).save()
        elif method == "create":
            manager.create(name=candidate.name, is_active=candidate.is_active)
        elif method == "insert":
            manager.insert(candidate)
        else:
            manager.bulk_create([candidate])

    if active:
        persist()
    else:
        with pytest.raises(PermissionDenied):
            persist()
    assert Folder._base_manager.count() == int(active)
    assert not Relationship.objects.exists()


@pytest.mark.parametrize(
    "expression,allowed",
    [
        ("public->read", False),
        ("authenticated - public->read", False),
        ("authenticated & public->read", False),
        ("authenticated + public->read", True),
    ],
)
@pytest.mark.parametrize("candidate", [None, Folder(name="unresolved", is_active=Value(True))])
def test_missing_or_unresolved_candidate_fails_closed(backend, expression, allowed, candidate):
    install_schema(
        backend,
        parse_zed(
            SCHEMA.replace("permission create = public->read", f"permission create = {expression}")
        ),
    )
    result = (
        check_new(subject=ACTOR, action="create", resource_type="blog/folder")
        if candidate is None
        else _check_new_model(candidate, subject=ACTOR, backend=backend)
    )
    assert result.allowed is allowed


def test_candidate_null_false_and_transforms_match_sql(backend):
    schema = SCHEMA.replace(
        '"is_active":true', '"parent_id":null,"is_active":false,"name__iexact":"public"'
    )
    install_schema(backend, parse_zed(schema))
    for name, active, expected in [
        ("PUBLIC", False, True),
        ("private", False, False),
        ("public", True, False),
    ]:
        candidate = Folder(name=name, is_active=active)
        assert _check_new_model(candidate, subject=ACTOR, backend=backend).allowed is expected
        with sudo(reason="compare candidate and stored row"):
            candidate.save()
        assert backend.has_access(subject=ACTOR, action="read", resource=ref(candidate)) is expected
        assert Folder.objects.with_actor(ACTOR).filter(pk=candidate.pk).exists() is expected


def test_null_comparison_does_not_pass_constraint_semantics(backend):
    install_schema(backend, parse_zed(SCHEMA.replace('"is_active":true', '"parent_id":123')))
    assert not _check_new_model(Folder(), subject=ACTOR, backend=backend).allowed


@pytest.mark.parametrize(
    "filters",
    [
        {"missing": True},
        {"parent__name": "public"},
        {"parent_id__name": "public"},
        {"posts__title": "public"},
    ],
)
def test_invalid_or_related_column_filters_report_e009_and_fail_closed(backend, filters):
    backend.set_schema(parse_zed(SCHEMA.replace('{"is_active":true}', json.dumps(filters))))
    assert any(issue.id == "rebac.E009" for issue in check_field_backed_relations())
    with pytest.raises(SchemaError, match=r"rebac\.E013"):
        backend.has_access(subject=ACTOR, action="read", resource=ObjectRef("blog/folder", "1"))
    with pytest.raises(SchemaError, match=r"rebac\.E013"):
        list(Folder.objects.with_actor(ACTOR))
    with pytest.raises(ValueError, match="const backing"):
        _check_new_model(Folder(), subject=ACTOR, backend=backend)
    # This arrow reaches a persisted target, so its index read also fails closed.
    with pytest.raises(SchemaError, match=r"rebac\.E013"):
        check_new(subject=ACTOR, action="unrestricted", resource_type="blog/folder")


def test_projected_filtered_constant_overlay_and_bare_constant_guard(backend):
    import inspect

    assert tuple(inspect.signature(check_new).parameters) == (
        "subject",
        "action",
        "resource_type",
        "relationships",
        "backend",
        "context",
    )
    for facts, expected in [
        ((SubjectRef.of("site/audience", "public"),), True),
        ((), False),
        (None, False),
    ]:
        assert (
            check_new(
                subject=ACTOR,
                action="create",
                resource_type="blog/folder",
                relationships={"public": facts},
            ).allowed
            is expected
        )
    with pytest.raises(SchemaError, match="const-backed"):
        check_new(
            subject=ACTOR,
            action="unrestricted",
            resource_type="blog/folder",
            relationships={"all_rows": [SubjectRef.of("site/audience", "public")]},
        )


@pytest.mark.parametrize("facts", [(), None, (SubjectRef.of("site/audience", "public"),)])
def test_model_hook_cannot_supply_filtered_constants(backend, monkeypatch, facts):
    monkeypatch.setattr(Folder, "proposed_relationships", lambda self, **kwargs: {"public": facts})
    with pytest.raises(SchemaError, match="library-owned"):
        _check_new_model(Folder(), subject=ACTOR, backend=backend)


def test_persisted_schema_keeps_filtered_constant(backend):
    definition = SchemaDefinition.objects.create(resource_type="blog/folder")
    SchemaRelation.objects.create(
        definition=definition,
        name="public",
        allowed_subjects=[{"type": "site/audience"}],
        backing={"kind": "const", "target_id": "public", "filters": {"is_active": True}},
    )
    SchemaPermission.objects.create(definition=definition, name="read", expression="public->read")
    audience = SchemaDefinition.objects.create(resource_type="site/audience")
    SchemaPermission.objects.create(definition=audience, name="read", expression="authenticated")
    reset_backend()
    backend = active_backend()
    rebuild_backend(backend)
    with sudo(reason="persisted schema fixture"):
        matching = Folder.objects.create(name="public")
        hidden = Folder.objects.create(name="private", is_active=False)
    assert backend.has_access(subject=ACTOR, action="read", resource=ref(matching))
    assert not backend.has_access(subject=ACTOR, action="read", resource=ref(hidden))


@pytest.mark.django_db(transaction=True)
def test_constant_arrow_to_filtered_target_checks_the_target_row(backend):
    with sudo(reason="nested constant fixtures"):
        folder = Folder.objects.create(name="target")
        post = Post.objects.create(title="source")
    schema = SCHEMA.replace(
        "relation folder: blog/folder // rebac:field=folder",
        f"relation folder: blog/folder // rebac:const={folder.pk}",
    )
    install_schema(backend, parse_zed(schema))
    with evaluator_scope() as evaluator:
        assert backend._cache_generation("blog/post") is None
        assert backend._cache_generation("blog/folder") is None
        for active in (True, False):
            pending = Post.objects.with_actor(ACTOR)
            Folder.objects.sudo(reason="backing fixture update").filter(pk=folder.pk).update(
                is_active=active
            )
            assert (
                evaluator.check(backend, subject=ACTOR, action="read", resource=ref(post)).allowed
                is active
            )
            with patch.object(
                backend, "accessible", side_effect=AssertionError("unexpected enumeration")
            ):
                assert pending.exists() is active
    assert not Relationship.objects.exists()


def test_direct_constant_scope_remains_lazy(backend):
    with sudo(reason="direct constant fixture"):
        folder = Folder.objects.create(name="source")
    actor = SubjectRef.of("site/audience", "public")
    assert list(
        Folder.objects.with_actor(actor).with_action("public").values_list("pk", flat=True)
    ) == [folder.pk]
    pending = Folder.objects.with_actor(actor).with_action("public")
    Folder.objects.sudo(reason="backing fixture update").filter(pk=folder.pk).update(
        is_active=False
    )
    assert not pending.exists()


def test_candidate_filter_runs_without_any_source_rows(backend, django_assert_num_queries):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    assert not Folder._base_manager.exists()
    with CaptureQueriesContext(connection) as queries, django_assert_num_queries(3):
        assert _check_new_model(Folder(), subject=ACTOR, backend=backend, using="default").allowed
    assert "FROM" not in queries[0]["sql"].upper()


def test_database_default_is_unknown_and_database_errors_propagate(backend):
    from django.db import DatabaseError
    from django.db.models.expressions import DatabaseDefault

    assert not _check_new_model(
        Folder(is_active=DatabaseDefault(Value(True))), subject=ACTOR, backend=backend
    ).allowed
    with (
        patch(
            "django.db.models.sql.compiler.SQLCompiler.execute_sql",
            side_effect=DatabaseError("unavailable"),
        ),
        pytest.raises(DatabaseError),
    ):
        _check_new_model(Folder(), subject=ACTOR, backend=backend)


def test_filtered_constant_export_is_byte_deterministic_and_legacy_compatible(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from .test_build_zed import _run_build

    app_dir = tmp_path / "source"
    app_dir.mkdir()
    source = app_dir / "permissions.zed"
    source.write_text(SCHEMA)
    app = SimpleNamespace(
        name="testapp", label="testapp", path=str(app_dir), rebac_schema="permissions.zed"
    )
    first = _run_build(monkeypatch, [app], tmp_path / "first.zed")
    second = _run_build(monkeypatch, [app], tmp_path / "second.zed")
    assert first == second
    assert "use typechecking" in first
    assert "rebac:const" not in first
    source.write_text(
        SCHEMA.replace('const={"target_id":"public","filters":{"is_active":true}}', "const=public")
    )
    assert _run_build(monkeypatch, [app], tmp_path / "legacy.zed") == first


@pytest.mark.pg_delta
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("backing_kind", ["const", "field"])
def test_candidate_column_collation_matches_stored_checks_and_exclusion(
    backend, monkeypatch, backing_kind
):
    from django.db import connection, models
    from django.test.utils import isolate_apps

    from rebac import RebacMixin, field_backing

    if connection.vendor != "sqlite":
        pytest.skip("SQLite's named nocase collation")
    with isolate_apps("tests.testapp"):

        class CollatedRow(RebacMixin, models.Model):
            audience = models.CharField(max_length=32, db_collation="nocase")
            target = models.ForeignKey(Folder, on_delete=models.CASCADE)

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "test/collatedrow"

        # isolate_apps models are absent from the global model registry used by the resolver.
        original_resolver = field_backing.model_for_resource_type
        monkeypatch.setattr(
            field_backing,
            "model_for_resource_type",
            lambda name: CollatedRow if name == "test/collatedrow" else original_resolver(name),
        )
        directive = (
            'const={"target_id":"public","filters":{"audience":"PUBLIC"}}'
            if backing_kind == "const"
            else 'field={"path":"target","filters":{"audience":"PUBLIC"}}'
        )
        backend.set_schema(
            parse_zed(
                """
            definition blog/folder { permission read = authenticated }
            definition test/collatedrow {
                relation public: blog/folder // rebac:"""
                + directive
                + """
                permission read = public->read
                permission create = authenticated - public->read
            }
        """
            )
        )
        with connection.schema_editor() as editor:
            editor.create_model(CollatedRow)
        try:
            rebuild_backend(backend)
            with sudo(reason="column collation target"):
                target = Folder.objects.create(name="target")
            for audience, matches in [("public", True), ("private", False)]:
                candidate = CollatedRow(audience=audience, target=target)
                assert (
                    _check_new_model(candidate, subject=ACTOR, backend=backend).allowed
                    is not matches
                )
                if matches:
                    with pytest.raises(PermissionDenied):
                        CollatedRow.objects.with_actor(ACTOR).insert(candidate)
                candidate.sudo(reason="column collation fixture").save()
                assert (
                    backend.has_access(subject=ACTOR, action="read", resource=ref(candidate))
                    is matches
                )
                assert (
                    CollatedRow.objects.with_actor(ACTOR).filter(pk=candidate.pk).exists()
                    is matches
                )
        finally:
            with connection.schema_editor() as editor:
                editor.delete_model(CollatedRow)


@pytest.mark.parametrize("wire_id", ["abc", "", "1.5", "item-abc"])
@pytest.mark.parametrize("kind", ["const", "field"])
def test_malformed_source_ids_deny_for_every_backing(backend, wire_id, kind):
    if kind == "const":
        resource = ObjectRef("blog/folder", wire_id)
        relation, actor = "public", SubjectRef.of("site/audience", "public")
    else:
        resource = ObjectRef("blog/post", wire_id)
        relation, actor = "folder", SubjectRef.of("blog/folder", "1")
    assert not backend.has_access(subject=ACTOR, action="read", resource=resource)
    assert not backend.has_access(subject=actor, action=relation, resource=resource)
    assert (
        list(
            backend.lookup_subjects(
                resource=resource, action=relation, subject_type=actor.subject_type
            )
        )
        == []
    )


def test_filtered_constants_reject_inherited_columns_but_accept_mti_pk(backend):
    from rebac.field_backing import resolve_const_backing
    from tests.testapp.models import NativeParentLinkedChild

    text = """
        definition site/audience { permission read = authenticated }
        definition test/nativeparentlinkedchild {
            relation public: site/audience // rebac:const={"target_id":"public","filters":{"name":"parent column"}}
            permission read = public->read
            permission create = public->read
        }
    """
    backend.set_schema(parse_zed(text))
    issues = check_field_backed_relations()
    assert any(
        issue.id == "rebac.E009" and "local concrete fields" in issue.msg for issue in issues
    )
    with pytest.raises(ValueError, match="const backing"):
        _check_new_model(
            NativeParentLinkedChild(name="parent column"), subject=ACTOR, backend=backend
        )
    install_schema(backend, parse_zed(text.replace('"name":"parent column"', '"pk":7')))
    assert not [issue for issue in check_field_backed_relations() if issue.id == "rebac.E009"]
    definition = backend.schema().get_definition("test/nativeparentlinkedchild")
    resolved = resolve_const_backing(definition, definition.relations[0])
    assert resolved.matches_candidate(NativeParentLinkedChild(pk=7)) is True
    assert resolved.matches_candidate(NativeParentLinkedChild(pk=8)) is False
    assert resolved.matches_candidate(NativeParentLinkedChild()) is None


def test_pk_filter_accepts_proxy_model_identity(backend):
    from rebac.field_backing import resolve_const_backing
    from tests.testapp.models import VirtualFolder

    backend.set_schema(
        parse_zed("""
        definition site/audience { permission read = authenticated }
        definition test/virtualfolder {
            relation public: site/audience // rebac:const={"target_id":"public","filters":{"pk":7}}
        }
    """),
    )
    definition = backend.schema().get_definition("test/virtualfolder")
    resolved = resolve_const_backing(definition, definition.relations[0])
    assert resolved is not None
    assert resolved.matches_candidate(VirtualFolder(pk=7)) is True


@pytest.mark.parametrize("kind", ["field", "const"])
def test_unsorted_native_filters_have_canonical_equality(kind):
    from rebac.schema import AllowedSubject, Definition, FieldBinding, Relation, Schema

    binding_type = FieldBinding if kind == "field" else ConstBinding
    first = binding_type("parent", filters=(("name", "source"), ("is_active", True)))
    second = binding_type("parent", filters=(("is_active", True), ("name", "source")))
    assert first == second
    assert hash(first) == hash(second)
    first_schema = Schema(
        definitions=[
            Definition(
                "blog/folder",
                relations=(Relation("parent", (AllowedSubject("blog/folder"),), backing=first),),
                permissions=(),
            )
        ]
    )
    second_schema = Schema(
        definitions=[
            Definition(
                "blog/folder",
                relations=(Relation("parent", (AllowedSubject("blog/folder"),), backing=second),),
                permissions=(),
            )
        ]
    )
    assert render_zed(first_schema) == render_zed(second_schema)


def test_builtin_intersection_enumeration_and_scope_agree(backend, monkeypatch):
    install_schema(
        backend,
        parse_zed(
            SCHEMA.replace(
                "permission read = public->read", "permission read = authenticated & public->read"
            )
        ),
    )
    with sudo(reason="built-in enumeration fixture"):
        folder = Folder.objects.create(name="public")
    assert backend.has_access(subject=ACTOR, action="read", resource=ref(folder))
    assert set(backend.accessible(subject=ACTOR, action="read", resource_type="blog/folder")) == {
        str(folder.pk)
    }
    assert list(Folder.objects.with_actor(ACTOR)) == [folder]
    # A backend using the public enumeration fallback retains the same grants.
    monkeypatch.setattr(backend, "queryset_filter", lambda **kwargs: None)
    assert list(Folder.objects.with_actor(ACTOR)) == [folder]


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["const", "field"])
def test_candidate_and_stored_backing_use_requested_database(
    backend, django_db_blocker, tmp_path, kind
):
    from django.db import connections

    from rebac.field_backing import resolve_const_backing

    alias = "backing_target"
    from tests.backend_setup import sqlite_alias

    target = sqlite_alias(alias, tmp_path / "backing.sqlite3")
    connections[alias] = target
    try:
        with django_db_blocker.unblock():
            from django.core.management import call_command

            from rebac.index.read import check, using_backend
            from tests.backend_setup import rebuild_backend

            call_command("migrate", database=alias, verbosity=0)
            rebuild_backend(backend, using=alias)
            with sudo(reason="separate database fixture"):
                remote = Folder.objects.using(alias).create(name="remote", is_active=True)
                Folder.objects.using("default").create(pk=remote.pk, name="local", is_active=False)
            definition = backend.schema().get_definition("blog/folder")
            relation = next(r for r in definition.relations if r.name == "public")
            resolved = resolve_const_backing(definition, relation)
            assert resolved.matches(str(remote.pk), using=alias)
            assert not resolved.matches(str(remote.pk), using="default")
            for action, actor in [
                ("read", ACTOR),
                ("public", SubjectRef.of("site/audience", "public")),
            ]:
                with using_backend(backend):
                    assert check(
                        resource=ref(remote), action=action, actor=actor, context=None, using=alias
                    ).allowed
                    assert not check(
                        resource=ref(remote),
                        action=action,
                        actor=actor,
                        context=None,
                        using="default",
                    ).allowed
            if kind == "const":
                candidate = Folder(name="new")
            else:
                schema = SCHEMA.replace(
                    "permission read = folder->read",
                    "permission read = folder->read\n permission create = folder",
                ).replace(
                    "rebac:field=folder",
                    'rebac:field={"path":"folder","filters":{"folder__is_active":true}}',
                )
                install_schema(backend, parse_zed(schema))
                candidate = Post(title="new", folder_id=remote.pk)
                rebuild_backend(backend, using=alias)
                with sudo(reason="alias propagation through field arrow"):
                    post = Post.objects.using(alias).create(title="remote", folder=remote)
                with using_backend(backend):
                    assert check(
                        resource=ref(post), action="read", actor=ACTOR, context=None, using=alias
                    ).allowed
            from django.test.utils import CaptureQueriesContext

            actor = ACTOR if kind == "const" else SubjectRef.of("blog/folder", str(remote.pk))
            with CaptureQueriesContext(target) as queries:
                assert _check_new_model(
                    candidate, subject=actor, backend=backend, using=alias
                ).allowed
            assert len(queries) > 0
            if kind == "field":
                assert not _check_new_model(
                    candidate, subject=actor, backend=backend, using="default"
                ).allowed
    finally:
        target.close()
        del connections[alias]
