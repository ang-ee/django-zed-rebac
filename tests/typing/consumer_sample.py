"""Consumer-shaped code that both type checkers must accept (``make typecheck``).

pyright has no Django plugin: what it sees of ``Model.objects`` is what the
``RebacManager`` class declares. This module pins the verbs an editor user
reaches for, so a typing change that drops them fails tier 1 instead of
reaching every consumer's editor.
"""

from __future__ import annotations

from typing import assert_type

from django.db import models

from rebac import RebacManager, RebacQuerySet, SubjectRef
from tests.testapp.models import Post


class PostQuerySet(RebacQuerySet[Post]):
    def published(self) -> PostQuerySet:
        return self.filter(title__startswith="published")


PostManager = RebacManager.from_queryset(PostQuerySet)


def verbs_on_a_model_manager(actor: SubjectRef) -> None:
    assert_type(Post.objects.with_actor(actor).get(), Post)
    assert_type(Post.objects.as_user(actor).filter(title="x").first(), Post | None)
    assert_type(Post.objects.as_agent(actor, on_behalf_of=actor).count(), int)
    assert_type(Post.objects.with_action("write").for_write().get(), Post)
    assert_type(Post.objects.sudo(reason="sample").get(), Post)
    assert_type(Post.objects.system_context(reason="sample").get(), Post)
    assert_type(Post.objects.on_field_deny("redact").get(), Post)
    assert_type(Post.objects.rebac_select_related("author").get(), Post)
    assert_type(Post.objects.rebac_prefetch_related("tags").get(), Post)
    assert_type(Post.objects.readable("title"), models.Expression)
    assert_type(Post.objects.insert(Post(title="x")), Post)
    assert_type(Post.objects.actor(), SubjectRef | None)
    assert_type(Post.objects.is_sudo(), bool)
    assert_type(Post.objects.effective_actor(), tuple[SubjectRef | None, bool])
    assert_type(Post.objects.scoped().get(), Post)
    assert_type(Post.objects.scoped_for_aggregate().get(), Post)


def verbs_on_a_custom_queryset(actor: SubjectRef) -> None:
    rows = PostQuerySet(model=Post)
    assert_type(rows.with_actor(actor).published(), PostQuerySet)
    assert_type(rows.sudo(reason="sample").published().get(), Post)


def verbs_on_a_from_queryset_manager(actor: SubjectRef) -> None:
    # pyright sees the RebacManager declarations here; the custom queryset's
    # own methods and type are a mypy-plugin guarantee (tests/test_manager_typing.py).
    manager: RebacManager[Post] = PostManager()
    manager.model = Post
    assert_type(manager.with_actor(actor).count(), int)
    assert_type(manager.actor(), SubjectRef | None)
