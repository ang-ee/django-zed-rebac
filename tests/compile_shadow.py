"""Test-only comparison of source-backed index reads and the live compiler.

Enable with ``pytest --compiled-shadow``. Production never imports this module.
Comparisons run when lazy scopes compile, rather than when querysets are built.
The original read remains the returned result throughout the shadow phase.
The shared index.read seam also covers the existing source-reference harness;
conftest explicitly excludes synthetic index-only fixtures.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

from django.db import connections
from django.db.models import BooleanField, Expression, Q, Value

from rebac.types import CheckResult

_comparing = ContextVar("compiled_shadow_comparing", default=False)


@contextmanager
def comparison(using):
    token = _comparing.set(True)
    connection = connections[using]
    queries = tuple(connection.queries_log)
    try:
        # Both sides read the application clock when their statement runs.
        yield
    finally:
        # Existing query-count assertions measure the unchanged production path.
        # Compiled-query budgets have their own focused tests.
        connection.queries_log.clear()
        connection.queries_log.extend(queries)
        _comparing.reset(token)


def canonical(value):
    if isinstance(value, CheckResult):
        return value.result, value.allowed, tuple(sorted(value.conditional_on))
    if isinstance(value, bool):
        return value
    return frozenset(value)


def assert_equal(operation, kwargs, old, new):
    original = canonical(old)
    candidate = canonical(new)
    assert original == candidate, (
        f"compiled shadow mismatch in {operation}: {kwargs!r}\n"
        f"index: {original!r}\ncompiled: {candidate!r}"
    )


class ShadowScope(Expression):
    def __init__(self, predicate, candidate, *, model, subject, action, using):
        super().__init__(output_field=BooleanField())
        self.predicate = predicate
        self.original = predicate
        self.candidate = candidate
        self.model = model
        self.subject = subject
        self.action = action
        self.using = using

    def get_source_expressions(self):
        return [self.predicate]

    def set_source_expressions(self, expressions):
        [self.predicate] = expressions

    def as_sql(self, compiler, connection):
        if not _comparing.get():
            with comparison(self.using):
                rows = self.model._base_manager.using(self.using).order_by()
                old_ids = list(rows.filter(self.original).values_list("pk", flat=True))
                new_ids = list(rows.filter(self.candidate).values_list("pk", flat=True))
                assert_equal(
                    "queryset_filter",
                    {
                        "model": self.model._meta.label,
                        "subject": self.subject,
                        "action": self.action,
                    },
                    old_ids,
                    new_ids,
                )
        return compiler.compile(self.predicate)


class ShadowEnumeration(Expression):
    """Compare complete ID sets without making a lazy enumeration eager."""

    def __init__(self, original, candidate, kwargs):
        super().__init__(output_field=BooleanField())
        self.original = original
        self.candidate = candidate
        self.kwargs = kwargs

    def as_sql(self, compiler, connection):
        if not _comparing.get():
            with comparison(self.kwargs["using"]):
                assert_equal("accessible_ids", self.kwargs, self.original, self.candidate)
        return compiler.compile(Value(True, output_field=BooleanField()))


def install(monkeypatch):
    """Instrument the shared read seam, including direct source-oracle tests."""
    from rebac.compile import read
    from rebac.index import read as index_read

    scope = index_read.scope_q

    @wraps(scope)
    def scoped(model, *, actor, action, using):
        predicate = scope(model, actor=actor, action=action, using=using)
        if _comparing.get():
            return predicate
        with comparison(using):
            candidate = read.scope_q(
                backend=index_read._backend(), model=model, actor=actor, action=action, using=using
            )
        return Q(
            ShadowScope(
                predicate, candidate, model=model, subject=actor, action=action, using=using
            )
        )

    monkeypatch.setattr(index_read, "scope_q", scoped)

    def wrap(name, compiled):
        original = getattr(index_read, name)

        @wraps(original)
        def compared(**kwargs):
            old = original(**kwargs)
            if _comparing.get():
                return old
            candidate_args = dict(kwargs)
            candidate_args.setdefault("context", None)
            with comparison(kwargs["using"]):
                new = compiled(backend=index_read._backend(), **candidate_args)
                if name == "accessible_ids":
                    return old.filter(ShadowEnumeration(old, new, kwargs))
                assert_equal(name, kwargs, old, new)
            return old

        monkeypatch.setattr(index_read, name, compared)

    wrap("check", read.check)
    wrap("accessible_ids", read.accessible_ids)
    wrap("_grants_all", read.grants_all)
    wrap("lookup_subjects", read.lookup_subjects)
