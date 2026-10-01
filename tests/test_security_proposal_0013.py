"""Executable security gaps deferred to proposal 0013.

Each case here is a write path where the integration layer re-implements a
piece of Django's query semantics in Python and SQL disagrees with it. They
are closed by compiling the gate from the statement's own ``Query`` (proposal
0013), not by refining the emulation. Every test asserts the secure outcome
and is a strict expected failure until then.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import connection, models, transaction
from django.db.models import Case, Exists, F, Func, Q, Subquery, Value, When
from django.db.models.expressions import RawSQL
from django.test import override_settings
from django.test.utils import isolate_apps

from rebac import (
    MissingActorError,
    ObjectRef,
    PermissionDenied,
    RelationshipTuple,
    SubjectRef,
    actor_context,
    backend,
    sudo,
)
from rebac.backends import reset_backend
from rebac.schema import parse_zed
from tests.backend_setup import install_schema
from tests.test_security_related_writes import (
    EDITOR,
    REVERSE_FK_SCHEMA,
    REVERSE_M2M_SCHEMA,
    SCHEMA_TEXT,
    _grant,
)
from tests.testapp.models import Folder, Post

A = SubjectRef.of("auth/user", "a")
B = SubjectRef.of("auth/user", "b")
INT = models.IntegerField()
CHAR = models.CharField()
TABLE = Post._meta.db_table
BODY = f'"{TABLE}"."body"'

PINNED = "proposal 0013: "


@pytest.fixture(autouse=True)
def isolated_backend(db):
    reset_backend()
    yield
    reset_backend()


def _folder_read(subject, folder):
    return backend().has_access(
        subject=subject, action="read", resource=ObjectRef("blog/folder", str(folder.pk))
    )


# --- A crafted Case passes the bulk_update shape check but SQL writes another arm ---


def _case_world():
    install_schema(backend(), parse_zed(REVERSE_FK_SCHEMA))
    with sudo(reason="fixture"):
        posts = [Post.objects.create(title=f"p{i}") for i in range(2)]
        folders = [Folder.objects.create(name=f"f{i}") for i in range(2)]
    for post in posts:
        _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", folders[0].pk, "owner", EDITOR)
    return posts, folders


def _case_shapes(posts, folders):
    mine, theirs = folders[0].pk, folders[1].pk
    exact = [When(pk=post.pk, then=Value(mine)) for post in posts]
    return {
        "negated_arm_first": Case(When(~Q(pk=-1), then=Value(theirs)), *exact, output_field=INT),
        "f_pk_arm_first": Case(When(pk=F("pk"), then=Value(theirs)), *exact, output_field=INT),
        "string_pk_arm_first": Case(
            *[When(pk=str(post.pk), then=Value(theirs)) for post in posts],
            *exact,
            output_field=INT,
        ),
        "value_pk_arm_first": Case(
            *[When(pk=Value(post.pk), then=Value(theirs)) for post in posts],
            *exact,
            output_field=INT,
        ),
    }


@pytest.mark.xfail(
    strict=True,
    reason=PINNED + "the Python CASE emulation resolves a different arm than SQL",
)
@pytest.mark.parametrize(
    "shape", ["negated_arm_first", "f_pk_arm_first", "string_pk_arm_first", "value_pk_arm_first"]
)
@pytest.mark.parametrize("path", ["scoped", "base_manager"])
def test_crafted_case_cannot_move_posts_into_an_unwritable_folder(shape, path):
    posts, folders = _case_world()
    expression = _case_shapes(posts, folders)[shape]
    pks = [post.pk for post in posts]
    with pytest.raises(PermissionDenied), transaction.atomic():
        if path == "scoped":
            Post.objects.with_actor(EDITOR).filter(pk__in=pks).update(folder=expression)
        else:
            with actor_context(EDITOR):
                Post._base_manager.filter(pk__in=pks).update(folder=expression)
    assert not _folder_read(EDITOR, folders[1])


@pytest.mark.xfail(
    strict=True,
    reason=PINNED + "the Python CASE emulation resolves a different arm than SQL",
)
def test_crafted_case_cannot_relink_a_through_row():
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    with sudo(reason="fixture"):
        post = Post.objects.create(title="p")
        mine = Folder.objects.create(name="mine")
        theirs = Folder.objects.create(name="theirs")
        post.collections.add(mine)
    _grant("blog/post", post.pk, "owner", EDITOR)
    _grant("blog/folder", mine.pk, "owner", EDITOR)
    through = Post.collections.through
    row = through.objects.get(post_id=post.pk)
    with pytest.raises(PermissionDenied), transaction.atomic(), actor_context(EDITOR):
        through.objects.filter(pk=row.pk).update(
            folder_id=Case(
                When(~Q(pk=-1), then=Value(theirs.pk)),
                When(pk=row.pk, then=Value(mine.pk)),
                output_field=INT,
            )
        )
    assert not through.objects.filter(post_id=post.pk, folder_id=theirs.pk).exists()


# --- Literal SQL reaches a write through a Q or an annotation the walker never sees ---


def _oracle(condition):
    return Case(
        When(condition, then=Value("ORACLE:yes")), default=Value("ORACLE:no"), output_field=CHAR
    )


def _q_vectors():
    secret_ids = RawSQL(f'SELECT id FROM "{TABLE}" WHERE body LIKE %s', ["secret%"])
    return {
        "when_q_rawsql_in": _oracle(Q(pk__in=secret_ids)),
        "when_kwarg_rawsql": Case(
            When(
                title=RawSQL(f"(SELECT 'public' FROM \"{TABLE}\" WHERE body LIKE 'secret%%')", []),
                then=Value("ORACLE:yes"),
            ),
            default=Value("ORACLE:no"),
            output_field=CHAR,
        ),
        "when_q_exists_extra": _oracle(
            Q(Exists(ContentType.objects.extra(where=[f"instr({BODY}, 'secret') > 0"])))
        ),
        "when_q_func_template": _oracle(
            Q(
                title=Func(
                    F("title"),
                    template=f"(CASE WHEN {BODY} LIKE 'secret%%' THEN 'public' END)",
                    output_field=CHAR,
                )
            )
        ),
    }


def _gated_post():
    install_schema(backend(), parse_zed(SCHEMA_TEXT))
    with sudo(reason="fixture"):
        post = Post.objects.create(title="public", body="secret body")
    _grant("blog/post", post.pk, "editor", EDITOR)
    return post


@pytest.mark.xfail(
    strict=True,
    reason=PINNED + "the expression walker does not traverse Q objects inside a When",
)
@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("name", sorted(_q_vectors()))
@pytest.mark.parametrize("method", ["update", "save"])
def test_literal_sql_inside_a_when_condition_is_refused(name, method):
    post = _gated_post()
    expression = _q_vectors()[name]
    with pytest.raises(PermissionDenied), transaction.atomic():
        if method == "update":
            Post.objects.with_actor(EDITOR).filter(pk=post.pk).update(title=expression)
        else:
            row = Post.objects.with_actor(EDITOR).get(pk=post.pk)
            row.title = expression
            row.save(update_fields=["title"])
    assert Post._base_manager.get(pk=post.pk).title == "public"


ANNOTATIONS = {
    "annotate_rawsql": {"annotate": RawSQL(BODY, [], output_field=CHAR)},
    "alias_rawsql": {"alias": RawSQL(BODY, [], output_field=CHAR)},
    "annotate_func_template": {"annotate": Func(F("title"), template=BODY, output_field=CHAR)},
    "annotate_subquery_extra": {
        "annotate": Subquery(
            ContentType.objects.extra(select={"v": BODY}).values("v")[:1], output_field=CHAR
        )
    },
}


@pytest.mark.xfail(
    strict=True,
    reason=PINNED + "an F() over a destination annotation is checked unresolved",
)
@override_settings(REBAC_FIELD_READ_MODE="redact")
@pytest.mark.parametrize("name", sorted(ANNOTATIONS))
def test_literal_sql_behind_a_destination_annotation_is_refused(name):
    post = _gated_post()
    queryset = Post.objects.with_actor(EDITOR).filter(pk=post.pk)
    if "annotate" in ANNOTATIONS[name]:
        queryset = queryset.annotate(b=ANNOTATIONS[name]["annotate"])
    else:
        queryset = queryset.alias(b=ANNOTATIONS[name]["alias"])
    with pytest.raises(PermissionDenied), transaction.atomic():
        queryset.update(title=F("b"))
    assert Post._base_manager.get(pk=post.pk).title == "public"


# --- A multi-table child writes through its parent's M2M without the parent's gate ---

POST_SIDE_SCHEMA = """
definition auth/user {}
definition blog/folder {
    relation owner: auth/user
    permission read = owner
    permission write = owner
}
definition blog/post {
    relation owner: auth/user
    relation collections: blog/folder // rebac:field=collections
    permission read = owner + collections->read
    permission write = owner
}
"""


@pytest.mark.xfail(
    strict=True,
    reason=PINNED + "the M2M gate keys affected rows by the child's concrete model",
)
@pytest.mark.django_db(transaction=True)
def test_multi_table_child_add_needs_write_on_the_post():
    install_schema(backend(), parse_zed(POST_SIDE_SCHEMA))
    with isolate_apps("tests.testapp"):

        class ChildPost(Post):
            extra = models.CharField(max_length=10, default="")

            class Meta:
                app_label = "testapp"
                rebac_resource_type = "blog/post"

        with connection.schema_editor() as editor:
            editor.create_model(ChildPost)
        try:
            with sudo(reason="fixture"):
                child = ChildPost.objects.create(title="b's child")
                folder = Folder.objects.create(name="a's")
            _grant("blog/post", child.pk, "owner", B)
            _grant("blog/folder", folder.pk, "owner", A)
            raw = ChildPost._base_manager.get(pk=child.pk)
            with pytest.raises(PermissionDenied), transaction.atomic(), actor_context(A):
                raw.collections.add(folder)
            assert not Post.collections.through.objects.filter(post_id=child.pk).exists()
        finally:
            with sudo(reason="cleanup"):
                Post.collections.through.objects.all().delete()
            with connection.schema_editor() as editor:
                editor.delete_model(ChildPost)


# --- Base-manager writes the docs used to claim were gated ---


@pytest.mark.xfail(
    strict=True,
    reason=PINNED + "the auto-created through model's _base_manager is a plain Manager",
)
@pytest.mark.parametrize("operation", ["bulk_create", "update", "delete"])
def test_through_base_manager_queryset_write_needs_folder_write(operation):
    install_schema(backend(), parse_zed(REVERSE_M2M_SCHEMA))
    through = Post.collections.through
    with sudo(reason="fixture"):
        post = Post.objects.create(title="a's")
        mine = Folder.objects.create(name="mine")
        victim = Folder.objects.create(name="victim")
        if operation != "bulk_create":
            post.collections.add(victim if operation == "delete" else mine)
    _grant("blog/post", post.pk, "owner", A)
    _grant("blog/folder", mine.pk, "owner", A)
    _grant("blog/folder", victim.pk, "owner", B)
    base = through._base_manager
    with pytest.raises(PermissionDenied), transaction.atomic(), actor_context(A):
        if operation == "bulk_create":
            base.bulk_create([through(post_id=post.pk, folder_id=victim.pk)])
        elif operation == "update":
            base.filter(post_id=post.pk).update(folder_id=victim.pk)
        else:
            base.filter(post_id=post.pk, folder_id=victim.pk).delete()
    linked = through.objects.filter(post_id=post.pk, folder_id=victim.pk).exists()
    assert linked == (operation == "delete")


CONST_FILTER_SCHEMA = """
definition auth/user {}
definition site/audience {
    relation member: auth/user
    permission read = member
}
definition blog/folder {
    relation owner: auth/user
    relation public: site/audience // rebac:const={"target_id":"public","filters":{"is_active":true}}
    permission read = owner + public->read
    permission write = owner
}
"""


@pytest.mark.xfail(
    strict=True,
    reason=PINNED + "a RebacMixin base-manager update gates FK columns only",
)
def test_base_manager_update_of_a_watched_scalar_column_needs_write():
    install_schema(backend(), parse_zed(CONST_FILTER_SCHEMA))
    with sudo(reason="fixture"):
        folder = Folder.objects.create(name="b's", is_active=False)
    backend().write_relationships(
        [
            RelationshipTuple(ObjectRef("blog/folder", str(folder.pk)), "owner", B),
            RelationshipTuple(ObjectRef("site/audience", "public"), "member", A),
        ]
    )
    assert not _folder_read(A, folder)
    with (
        pytest.raises((PermissionDenied, MissingActorError)),
        transaction.atomic(),
        actor_context(A),
    ):
        Folder._base_manager.filter(pk=folder.pk).update(is_active=True)
    assert not _folder_read(A, folder)
