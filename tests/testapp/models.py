"""Synthetic models for testing the mixin."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from rebac import RebacMixin

from .fields import EncodedIntegerField


class Folder(RebacMixin, models.Model):
    name = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/folder"


class Post(RebacMixin, models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="posts"
    )

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/post"


class SluggedPost(RebacMixin, models.Model):
    """Exercises ``Meta.rebac_id_attr`` — REBAC keys on ``slug``, not ``pk``.

    Mirrors the shape of an Angee model with a ``sqid`` field: an
    auto-PK Django row plus a stable, public, string id used as the
    REBAC resource_id. Tests in ``tests/test_id_attr.py`` round-trip
    relationship rows + manager scoping through the slug column.
    """

    slug = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=200)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/sluggedpost"
        rebac_id_attr = "slug"


class AuthoredPost(RebacMixin, models.Model):
    title = models.CharField(max_length=200)
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="authored_posts"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="authored_test_posts",
    )

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/authoredpost"


class EncodedFolder(RebacMixin, models.Model):
    public_id = EncodedIntegerField(unique=True)
    name = models.CharField(max_length=100)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/encodedfolder"
        rebac_id_attr = "public_id"


class EncodedPost(RebacMixin, models.Model):
    public_id = EncodedIntegerField(unique=True)
    title = models.CharField(max_length=200)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    folder = models.ForeignKey(EncodedFolder, null=True, on_delete=models.SET_NULL)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/encodedpost"
        rebac_id_attr = "public_id"


class EncodedPrimaryPost(RebacMixin, models.Model):
    id = EncodedIntegerField(primary_key=True)
    title = models.CharField(max_length=200)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    folder = models.ForeignKey(EncodedFolder, null=True, on_delete=models.SET_NULL)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/encodedprimarypost"
