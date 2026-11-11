"""Candidate-root reads agree with the unbounded scoped predicate."""

import pytest
from django.db.models import Count, Exists, OuterRef, Q
from django.test import override_settings

from rebac import ObjectRef, RelationshipTuple, SubjectRef, sudo, to_object_ref
from rebac.compile import read
from rebac.testing import install_schema
from tests.testapp.models import Folder, Post

pytestmark = [pytest.mark.django_db, pytest.mark.pg_delta]
A = SubjectRef.of("auth/user", "alice")
SCHEMA = """
caveat flagged(flag bool) { flag }
caveat needs(now int) { now > 0 }
definition auth/user {}
definition auth/group { relation member: auth/user | auth/group#member }
definition blog/folder {
    relation parent: blog/folder // rebac:field=parent
    relation viewer: auth/user | auth/group#member
    relation banned: auth/user
    permission read = (viewer + parent->read) - banned
}
definition blog/post {
    relation folder: blog/folder // rebac:field=folder
    relation viewer: auth/user | auth/group#member | auth/user with flagged | auth/user with needs
    relation banned: auth/user | auth/group#member
    permission read = (viewer + folder->read) - banned
}
"""


def _general_identities(_model, _lhs, _where, _using):
    return None


@pytest.fixture(params=["denormalized", "registry"])
def world(request, monkeypatch):
    with override_settings(REBAC_LOCAL_BACKEND_STORAGE=request.param, REBAC_DEPTH_LIMIT=4):
        local = install_schema(SCHEMA)
        with sudo(reason="probe"):
            chain = []
            parent = None
            for depth in range(7):  # deeper than the depth limit
                parent = Folder.objects.create(name=f"chain{depth}", parent=parent)
                chain.append(parent)
            other = Folder.objects.create(name="other")
            banned_folder = Folder.objects.create(name="bannedf", parent=chain[0])
            folders = [*chain, other, banned_folder]
            posts = [
                Post.objects.create(title=f"p{i}", folder=folders[i % len(folders)])
                for i in range(90)
            ]
        tuples = [
            RelationshipTuple(
                to_object_ref(chain[0]), "viewer", SubjectRef.of("auth/group", "g2", "member")
            ),
            RelationshipTuple(
                ObjectRef("auth/group", "g2"), "member", SubjectRef.of("auth/group", "g1", "member")
            ),
            RelationshipTuple(ObjectRef("auth/group", "g1"), "member", A),
            RelationshipTuple(to_object_ref(banned_folder), "banned", A),
            RelationshipTuple(
                to_object_ref(posts[7]), "viewer", A
            ),  # 'other' folder post: direct grant
            RelationshipTuple(to_object_ref(posts[1]), "banned", A),
            RelationshipTuple(
                to_object_ref(posts[16]),
                "viewer",
                A,
                caveat_name="flagged",
                caveat_context={"flag": True},
            ),
            RelationshipTuple(
                to_object_ref(posts[25]),
                "viewer",
                A,
                caveat_name="flagged",
                caveat_context={"flag": False},
            ),
            RelationshipTuple(
                to_object_ref(posts[34]), "viewer", A, caveat_name="needs", caveat_context={}
            ),
        ]
        local.write_relationships(tuples)
        calls = []
        original = read._scope_statement

        def observed(operation, model, key, *args, **kwargs):
            calls.append(kwargs.get("slots"))
            return original(operation, model, key, *args, **kwargs)

        monkeypatch.setattr(read, "_scope_statement", observed)
        yield local, folders, posts, calls, monkeypatch


def _ids(qs):
    return sorted(qs.values_list("pk", flat=True))


def _shapes(posts, folders):
    p = [x.pk for x in posts]
    big = p[:60]
    return {
        "list2": (lambda q: q.filter(pk__in=p[:2]), True),
        "list5": (lambda q: q.filter(pk__in=p[:5]), True),
        "list50": (lambda q: q.filter(pk__in=big[:50]), True),
        "all90": (lambda q: q.filter(pk__in=p), True),
        "tuple": (lambda q: q.filter(pk__in=tuple(p[10:20])), True),
        "set": (lambda q: q.filter(pk__in=set(p[10:20])), True),
        "strings": (lambda q: q.filter(pk__in=[str(x) for x in p[10:20]]), True),
        "qs_base": (
            lambda q: q.filter(pk__in=Post._base_manager.filter(pk__in=p[:30]).values("pk")),
            False,
        ),
        "qs_scoped": (
            lambda q: q.filter(
                pk__in=Post.objects.with_actor(A).filter(pk__in=p[:30]).values("pk")
            ),
            None,
        ),
        "mixed_col": (lambda q: q.filter(pk__in=p[:40], title__endswith="1"), True),
        "mixed_join": (lambda q: q.filter(pk__in=p[:40], folder__name__startswith="chain"), True),
        "or": (lambda q: q.filter(Q(pk__in=p[:5]) | Q(title="p40")), False),
        "exclude": (lambda q: q.exclude(pk__in=p[:5]), False),
        "neg_nested": (lambda q: q.filter(Q(title__startswith="p") & ~Q(pk__in=p[:5])), False),
        "neg_double": (lambda q: q.filter(~Q(~Q(pk__in=p[:5]))), False),
        "in_then_exclude": (lambda q: q.filter(pk__in=p[:30]).exclude(pk__in=p[:5]), True),
        "in_and_in": (lambda q: q.filter(pk__in=p[:30]).filter(pk__in=p[20:50]), True),
        "pk_and_in": (lambda q: q.filter(pk=p[7], pk__in=p[:30]), None),
        "dupes": (lambda q: q.filter(pk__in=[p[7], p[7], p[8]]), False),
        "null": (lambda q: q.filter(pk__in=[p[7], None, p[8]]), False),
        "sliced": (lambda q: q.filter(pk__in=p[:40]).order_by("-pk")[:5], True),
        "folder_related_in": (lambda q: q.filter(folder__in=folders[:3]), False),
    }


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param(name, marks=pytest.mark.slow)
        if name
        in {
            "list50",
            "all90",
            "qs_base",
            "qs_scoped",
            "mixed_join",
            "or",
            "exclude",
            "neg_nested",
            "neg_double",
            "in_then_exclude",
            "in_and_in",
            "dupes",
            "null",
            "folder_related_in",
        }
        else name
        for name in _shapes([Post(pk=i) for i in range(90)], [Folder(pk=i) for i in range(9)])
    ],
)
def test_bounded_equals_unbounded_intersection(world, shape):
    _local, folders, posts, calls, monkeypatch = world
    with override_settings(REBAC_DEPTH_LIMIT=4):
        full = set(_ids(Post.objects.with_actor(A)))
        builder, bounded = _shapes(posts, folders)[shape]
        with sudo(reason="probe"):
            selected = set(_ids(builder(Post._base_manager.all())))
        calls.clear()
        got = _ids(builder(Post.objects.with_actor(A)))
        lane_calls = list(calls)
        # The base (pre-lane) plan for the same shape.
        monkeypatch.setattr(read, "_known_identities", _general_identities)
        read.reset()
        base = _ids(builder(Post.objects.with_actor(A)))
    assert got == base, (shape, got, base)
    if shape != "sliced":
        assert got == sorted(full & selected), (shape, got, sorted(full & selected))
    used = any(c is not None for c in lane_calls)
    if bounded is not None:
        assert used is bounded, (shape, lane_calls)
    # Sanity: the fixture has visible, depth-cut, banned and caveated rows.
    assert posts[0].pk in full and posts[7].pk in full and posts[16].pk in full
    assert posts[1].pk not in full and posts[25].pk not in full and posts[34].pk not in full
    assert posts[6].pk not in full  # chain6 > depth limit 4


@pytest.mark.parametrize(
    "terminal",
    [
        "count",
        "exists",
        "in_bulk",
        "values_group",
        "scoped",
        "aggregate_scope",
        "union",
        pytest.param("embedded_in", marks=pytest.mark.slow),
        pytest.param("embedded_exists", marks=pytest.mark.slow),
        "folders_root",
    ],
)
def test_terminals_and_embeddings(world, terminal):
    _local, folders, posts, calls, monkeypatch = world
    p = [x.pk for x in posts]
    a, b = p[:20], p[30:60]
    with override_settings(REBAC_DEPTH_LIMIT=4):

        def run():
            rows = Post.objects.with_actor(A)
            if terminal == "count":
                return rows.filter(pk__in=a).count()
            if terminal == "exists":
                return [
                    rows.filter(pk__in=[p[1], p[25]]).exists(),
                    rows.filter(pk__in=[p[0], p[25]]).exists(),
                ]
            if terminal == "in_bulk":
                return sorted(rows.in_bulk(a))
            if terminal == "values_group":
                return sorted(
                    rows.filter(pk__in=a)
                    .values("folder")
                    .annotate(n=Count("pk"))
                    .values_list("folder", "n")
                )
            if terminal == "scoped":
                return _ids(rows.scoped().filter(pk__in=a))
            if terminal == "aggregate_scope":
                return _ids(rows.filter(pk__in=a).scoped_for_aggregate())
            if terminal == "union":
                return sorted(
                    rows.filter(pk__in=a)
                    .values_list("pk", flat=True)
                    .union(rows.filter(pk__in=b).values_list("pk", flat=True))
                )
            if terminal == "embedded_in":
                return _ids(Post._base_manager.filter(pk__in=rows.filter(pk__in=a).values("pk")))
            if terminal == "embedded_exists":
                return _ids(
                    Folder._base_manager.filter(
                        Exists(rows.filter(pk__in=b, folder=OuterRef("pk")))
                    )
                )
            if terminal == "folders_root":
                return _ids(Folder.objects.with_actor(A).filter(pk__in=[f.pk for f in folders]))

        calls.clear()
        lane = run()
        monkeypatch.setattr(read, "_known_identities", _general_identities)
        read.reset()
        base = run()
    assert lane == base, (terminal, lane, base)


def test_held_matches_base(world):
    local, _folders, posts, calls, monkeypatch = world
    ids = [str(x.pk) for x in posts]
    with override_settings(REBAC_DEPTH_LIMIT=4):
        calls.clear()
        lane = read.held(
            backend=local,
            resource_type="blog/post",
            action="read",
            actor=A,
            ids=ids,
            using="default",
        )
        used = any(c is not None for c in calls)
        monkeypatch.setattr(read, "_known_identities", _general_identities)
        read.reset()
        base = read.held(
            backend=local,
            resource_type="blog/post",
            action="read",
            actor=A,
            ids=ids,
            using="default",
        )
    assert lane == base
    assert used, "a chunk within the candidate limit takes the bounded statement"


def test_bounded_statements_are_kept_per_power_of_two(world):
    _local, folders, _posts, calls, monkeypatch = world
    f = [x.pk for x in folders]
    compiled = []
    original = read._Operation.compiler

    def counted(self, *args, **kwargs):
        compiled.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(read._Operation, "compiler", counted)
    with override_settings(REBAC_DEPTH_LIMIT=4):
        read.reset()
        calls.clear()
        # Folder reads reach no caveat, so their statements are kept. Three
        # and four identities share the four-slot statement; the spare slot
        # repeats the last identity.
        first = _ids(Folder.objects.with_actor(A).filter(pk__in=f[:3]))
        built = len(compiled)
        second = _ids(Folder.objects.with_actor(A).filter(pk__in=f[3:7]))
        reused = len(compiled)
        third = _ids(Folder.objects.with_actor(A).filter(pk=f[7]))
        assert calls == [4, 4, 1]
        assert built, "the first bounded read compiles"
        assert reused == built, "a read in a kept bucket compiles nothing"
        assert len(compiled) > reused, "a one-slot read keeps its own statement"
        monkeypatch.setattr(read, "_known_identities", _general_identities)
        read.reset()
        full = set(_ids(Folder.objects.with_actor(A)))
    assert first == sorted(full & set(f[:3]))
    assert second == sorted(full & set(f[3:7]))
    assert third == sorted(full & {f[7]})
    assert full & set(f[:7]) and set(f[:7]) - full, "the folders hold visible and hidden rows"
