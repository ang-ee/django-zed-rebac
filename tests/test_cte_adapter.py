"""Pins for the django-cte adapter debt documented in ARCHITECTURE.md."""

import ast
import inspect
from hashlib import sha256
from importlib import import_module
from importlib.metadata import version

import pytest
from django.db import connection
from django_cte import CTE, with_cte

from rebac import share_id_sets
from rebac.compile import ctes
from tests.testapp.models import Post


@pytest.mark.parametrize(
    "module,digest",
    [
        ("django_cte.jitmixin", "082ea517dda0ff1e7ba0a6da019952ba397056f506527768ba62f123998aafd4"),
        ("django_cte.query", "6749a26393f18fd038c1860064ff5fca698587bb2a355aac54ec9a165aee60a5"),
    ],
)
def test_private_adapter_contract_requires_review_on_upgrade(module, digest):
    assert version("django-cte").split(".")[:2] == ["5", "0"], (
        "Re-audit the django-cte adapter before widening its minor pin"
    )
    # Ignore formatting/comments; changes to the private compiler, attachment,
    # alias handling or JIT reconstruction protocol require an explicit review.
    source = ast.dump(ast.parse(inspect.getsource(import_module(module))))
    assert sha256(source.encode()).hexdigest() == digest, (
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
    assert mro.index(ctes._StatementCompiler) < mro.index(ctes.CTECompiler)
    assert mro.count(ctes._StatementCompiler) == mro.count(ctes.CTECompiler) == 1
    assert compiler.query is query
