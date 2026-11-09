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
