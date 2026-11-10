"""Pins for the django-cte adapter debt documented in ARCHITECTURE.md."""

import inspect
import tokenize
from hashlib import sha256
from importlib import import_module
from importlib.metadata import version
from io import StringIO
from token import tok_name

import pytest
from django.db import connection
from django_cte import CTE, with_cte

from rebac import share_id_sets
from rebac.compile import ctes
from rebac.managers import _BypassAuditCompilerMixin
from tests.testapp.models import Post

pytestmark = pytest.mark.pg_delta


@pytest.mark.parametrize(
    "module,digest",
    [
        ("django_cte.jitmixin", "92b76e4bf163fdbddbf567d03e98d9e4c2144d8ff47ebab47ce749318060c6b3"),
        ("django_cte.query", "bd713940ba4ffed2b332c2e688fa15b4086c675c07e9cc838b98cce53c359f68"),
        ("django_cte.join", "7a31f925a6cd79f9ea5285c66ecdfb3add79e7e82468d39e99a833bcf9b45b53"),
        ("django_cte.cte", "f358c8f197a73bfefd067a7921dcd7957677e8ac97de01cb460a155d2b24f999"),
    ],
)
def test_private_adapter_contract_requires_review_on_upgrade(module, digest):
    assert version("django-cte").split(".")[:2] == ["5", "0"], (
        "Re-audit the django-cte adapter before widening its minor pin"
    )
    # Ignore formatting/comments; changes to the private compiler, attachment,
    # alias handling or JIT reconstruction protocol require an explicit review.
    source = inspect.getsource(import_module(module))
    tokens = tuple(
        (tok_name[item.type], "" if item.type == tokenize.INDENT else item.string)
        for item in tokenize.generate_tokens(StringIO(source).readline)
        if item.type not in {tokenize.COMMENT, tokenize.NL}
    )
    assert sha256(repr(tokens).encode()).hexdigest() == digest, (
        f"{module} changed: re-audit rebac.compile.ctes against django-cte before updating this pin"
    )


@pytest.mark.parametrize("order", ["rebac", "share_first", "cte_first"])
def test_statement_compiler_precedes_cte_compiler(order):
    app = CTE(Post._base_manager.values("pk"), name="mro_posts")
    if order == "rebac":
        rows = with_cte(app, select=Post.objects.all())
    elif order == "share_first":
        rows = with_cte(app, select=share_id_sets(Post._base_manager.all()))
    else:
        rows = share_id_sets(with_cte(app, select=Post._base_manager.all()))
    query = rows.query
    compiler = query.get_compiler(connection=connection)
    mro = type(compiler).__mro__
    assert (
        mro.index(_BypassAuditCompilerMixin)
        < mro.index(ctes._StatementCompiler)
        < mro.index(ctes.CTECompiler)
    )
    assert mro.count(_BypassAuditCompilerMixin) == 1
    assert mro.count(ctes._StatementCompiler) == mro.count(ctes.CTECompiler) == 1
    assert compiler.query is query


# Discover every root mixin, so a new jit_mixin(..., RootQuery subclass) must
# obey the same layout contract instead of relying on one happy mixing order.
_ROOT_MIXINS = [
    item
    for item in vars(ctes).values()
    if isinstance(item, type)
    and issubclass(item, ctes.RootQuery)
    and issubclass(item, ctes.JITMixin)
]


@pytest.mark.parametrize("root_mixin", _ROOT_MIXINS, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("upstream", ["CTEQuery", "NoAliasQuery"])
@pytest.mark.parametrize("base", ["_RebacQuery", "_BypassAuditQuery"])
@pytest.mark.parametrize("root_first", [False, True])
def test_every_root_mixin_supports_cte_and_no_alias_queries(root_mixin, upstream, base, root_first):
    from rebac import managers

    upstream_mixin = getattr(import_module("django_cte.query"), upstream)
    query = getattr(managers, base)(Post)
    mixins = [root_mixin, upstream_mixin] if root_first else [upstream_mixin, root_mixin]
    for mixin in mixins:
        ctes.jit_mixin(query, mixin)
    mro = type(query).__mro__
    assert mro.index(ctes.JITMixin) < mro.index(ctes.RootQuery)
    assert mro.count(ctes.RootQuery) == 1
    compiler = query.get_compiler(connection=connection)
    assert compiler.query is query
    compiler_mro = type(compiler).__mro__
    assert (
        compiler_mro.index(_BypassAuditCompilerMixin)
        < compiler_mro.index(ctes._StatementCompiler)
        < compiler_mro.index(ctes.CTECompiler)
    )


@pytest.mark.parametrize("change", ["reorder", "omit", "body", "params"])
def test_assembled_sql_rejects_changed_upstream_contract(monkeypatch, change):
    from rebac import SchemaError

    original = ctes.CTECompiler.as_sql

    def changed(self, *args, **kwargs):
        sql, params = original(self, *args, **kwargs)
        # Preserve the parameter order for text mutations so the text guard,
        # independently of the sentinel guard, must reject the change.
        if change == "reorder":
            first = connection.ops.quote_name("first_set") + " AS"
            second = connection.ops.quote_name("second_set") + " AS"
            sql = sql.replace(first, "temporary_set AS", 1)
            sql = sql.replace(second, first, 1).replace("temporary_set AS", second, 1)
        elif change == "omit":
            sql = sql.replace(connection.ops.quote_name("second_set") + " AS", "missing_set AS", 1)
        elif change == "body":
            sql += " /* unexpected suffix */"
        else:
            params = tuple(reversed(params))
        return sql, params

    first = Post._base_manager.filter(title="first").values("pk").query.sql_with_params()
    second = Post._base_manager.filter(title="second").values("pk").query.sql_with_params()
    definitions = (
        ctes.Definition("first_set", first[0], tuple(first[1]), b"first"),
        ctes.Definition("second_set", second[0], tuple(second[1]), b"second"),
    )
    ctes._assembled_sql.cache_clear()
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ctes.CTECompiler, "as_sql", changed)
            with pytest.raises(SchemaError, match="django-cte changed"):
                ctes.assemble(first, definitions, connection)
        # A rejected assembly must not enter the cache.
        assert ctes._assembled_sql.cache_info().currsize == 0
        assert ctes.assemble(first, definitions, connection)[1] == (
            *first[1],
            *second[1],
            *first[1],
        )
    finally:
        ctes._assembled_sql.cache_clear()
