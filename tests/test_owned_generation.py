"""Owned generations stay local to one alias and publish on owner success."""

import contextvars
import threading

import pytest
from django.db import connection, connections, models, transaction

from rebac import RelationshipTuple, SubjectRef, evaluator_scope, sudo, to_object_ref
from rebac.testing import install_schema
from rebac.watch import model_write, row_generation, row_generations

from .testapp.models import BackingStage, Folder, Post

ACTOR = SubjectRef.of("auth/user", "owner-actor")
SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user
    permission read = viewer + parent->read
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    permission read = folder->read
}
"""


def generation(model, using="default"):
    return dict(row_generations((model._meta.label_lower,), using=using))[model._meta.label_lower]


def tree():
    local = install_schema(SCHEMA)
    with sudo(reason="generation fixture"):
        top = Folder.objects.create(name="top")
        child = Folder.objects.create(name="child", parent=top)
        post = Post.objects.create(title="post", folder=child)
    local.write_relationships([RelationshipTuple(to_object_ref(top), "viewer", ACTOR)])
    return top, child, post


def visible():
    return set(Post.objects.with_actor(ACTOR).values_list("pk", flat=True))


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_changed_row_is_redecided_inside_owner():
    top, child, post = tree()
    with evaluator_scope() as evaluator:
        assert visible() == {post.pk}
        with model_write(model=Folder, using="default"):
            assert visible() == {post.pk}
            assert evaluator.stats()["row_decision_entries"] > 0
            with sudo(reason="move child"):
                Folder.objects.filter(pk=child.pk).update(parent=None)
            assert visible() == set()
            with sudo(reason="move child back"):
                child.refresh_from_db()
                child.parent = top
                child.save()
            assert visible() == {post.pk}
            with sudo(reason="delete child"):
                Folder.objects.filter(pk=child.pk).delete()
            assert visible() == set()
        assert visible() == set()


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_reused_queryset_rebuilds_after_change_inside_owner():
    _top, child, post = tree()
    rows = Post.objects.with_actor(ACTOR)
    with model_write(model=Folder, using="default"):
        assert {row.pk for row in rows} == {post.pk}
        before = row_generation()
        with sudo(reason="move child"):
            Folder.objects.filter(pk=child.pk).update(parent=None)
        assert row_generation() != before
        rows._result_cache = None
        assert {row.pk for row in rows} == set()


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_success_publishes_once_and_rollback_publishes_none():
    install_schema(SCHEMA)
    start = generation(Folder)
    with model_write(model=Folder, using="default"):
        with sudo(reason="generation fixture"):
            for index in range(5):
                Folder.objects.create(name=f"folder {index}")
        assert "@operation" in dict(row_generations((Folder._meta.label_lower,), using="default"))
    assert generation(Folder) == start + 1
    middle = generation(Folder)
    with pytest.raises(RuntimeError):
        with model_write(model=Folder, using="default"):
            with sudo(reason="rolled-back fixture"):
                Folder.objects.create(name="rolled back")
            raise RuntimeError("rollback")
    assert generation(Folder) == middle
    assert not Folder._base_manager.filter(name="rolled back").exists()
    assert "@operation" not in dict(row_generations((Folder._meta.label_lower,), using="default"))


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_nested_rollback_does_not_publish_its_model_bump():
    install_schema(SCHEMA)
    before = generation(Folder)
    with model_write(model=BackingStage, using="default"):
        with pytest.raises(RuntimeError):
            with model_write(model=Folder, using="default"):
                with sudo(reason="nested rollback fixture"):
                    Folder.objects.create(name="nested rolled back")
                raise RuntimeError("rollback")
    assert generation(Folder) == before
    assert not Folder._base_manager.filter(name="nested rolled back").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_unowned_tracked_save_invalidates_immediately():
    install_schema(SCHEMA)
    before = generation(BackingStage)
    values = {}
    for field in BackingStage._meta.concrete_fields:
        if field.primary_key or field.null or field.has_default():
            continue
        if isinstance(field, models.CharField):
            values[field.name] = "x"
        elif isinstance(field, models.IntegerField):
            values[field.name] = 1
    with sudo(reason="unowned fixture"):
        BackingStage._base_manager.create(**values)
    assert generation(BackingStage) == before + 1


@pytest.mark.django_db(databases=["default", "selected"], transaction=True)
@pytest.mark.pg_delta
def test_other_alias_committed_inside_failed_owner_is_published():
    install_schema(SCHEMA)
    before = generation(Folder, using="selected")
    with pytest.raises(RuntimeError):
        with model_write(model=Folder, using="default"):
            with sudo(reason="other alias fixture"):
                Folder.objects.using("selected").create(name="other alias")
            assert not connections["selected"].in_atomic_block
            raise RuntimeError("outer owner fails")
    assert Folder._base_manager.using("selected").filter(name="other alias").exists()
    assert generation(Folder, using="selected") == before + 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_copied_context_does_not_defer_another_connections_write():
    if connection.vendor != "postgresql":
        pytest.skip("requires independently committing connections")
    install_schema(SCHEMA)
    before = generation(Folder)
    errors = []

    def write_in_copied_context():
        try:
            with sudo(reason="copied context fixture"):
                Folder.objects.create(name="copied context")
        except Exception as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    with pytest.raises(RuntimeError):
        with model_write(model=Folder, using="default"):
            copied = contextvars.copy_context()
            thread = threading.Thread(target=lambda: copied.run(write_in_copied_context))
            thread.start()
            thread.join(timeout=10)
            assert not thread.is_alive()
            assert not errors
            raise RuntimeError("outer owner fails")
    assert Folder._base_manager.filter(name="copied context").exists()
    assert generation(Folder) == before + 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_commit_hook_raising_after_the_owner_committed_still_publishes():
    """The outermost owner committed; a commit hook then raises."""
    install_schema(SCHEMA)
    before = generation(Folder)

    def boom():
        raise RuntimeError("hook")

    with pytest.raises(RuntimeError, match="hook"):
        with model_write(model=Folder, using="default"):
            with sudo(reason="test.owned_generation.hook"):
                Folder.objects.create(name="committed then hook fails")
            transaction.on_commit(boom)
    assert Folder._base_manager.filter(name="committed then hook fails").exists()
    assert generation(Folder) == before + 1, "committed rows published no bump"


@pytest.mark.django_db(transaction=True)
@pytest.mark.pg_delta
def test_context_copied_inside_an_owner_and_run_after_it_publishes():
    """A context copied inside an owner and run after the owner exits on the same thread."""
    install_schema(SCHEMA)
    with model_write(model=Folder, using="default"):
        stale = contextvars.copy_context()
    before = generation(Folder)

    def later():
        with sudo(reason="test.owned_generation.later"):
            Folder.objects.create(name="later")

    stale.run(later)
    assert Folder._base_manager.filter(name="later").exists()
    assert generation(Folder) == before + 1, "a committed owned write published no bump"
