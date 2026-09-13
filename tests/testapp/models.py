"""Synthetic models for testing the mixin."""

from __future__ import annotations

from django.conf import settings
from django.db import models

from rebac import RebacMixin

from .fields import (
    ColumnlessIdentityField,
    EncodedIntegerField,
    LowercaseCharField,
    MissingLookupIdentityField,
    NonExpressionIdentityField,
    VirtualEncodedIdentityField,
)


class Folder(RebacMixin, models.Model):
    virtual_id = VirtualEncodedIdentityField()
    columnless_id = ColumnlessIdentityField()
    nonexpression_id = NonExpressionIdentityField()
    missing_lookup_id = MissingLookupIdentityField()
    name = models.CharField(max_length=100)
    kind = LowercaseCharField(max_length=32, blank=True, default="")
    is_active = models.BooleanField(default=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/folder"


class Post(RebacMixin, models.Model):
    virtual_id = VirtualEncodedIdentityField()
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    folder = models.ForeignKey(
        Folder, null=True, blank=True, on_delete=models.SET_NULL, related_name="posts"
    )
    collections = models.ManyToManyField(Folder, blank=True, related_name="collected_posts")

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/post"


class SluggedPost(RebacMixin, models.Model):
    """Exercises ``Meta.rebac_id_attr`` — REBAC keys on ``slug``, not ``pk``.

    An auto-PK Django row has a stable public string id used as the REBAC
    resource id. Tests round-trip relationship rows and manager scoping through
    the slug column.
    """

    slug = models.CharField(max_length=64, unique=True)
    title = models.CharField(max_length=200)

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "blog/sluggedpost"
        rebac_id_attr = "slug"


class SubjectContainer(SluggedPost):
    """Proxy exercising model-owned subject-set identity without another table."""

    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "blog/subjectcontainer"
        rebac_id_attr = "slug"
        rebac_subject_relation = "member"


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
    role = models.CharField(max_length=32, blank=True, default="")
    confirmed = models.BooleanField(default=False)
    dismissed = models.BooleanField(default=False)

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


class VirtualFolder(Folder):
    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "test/virtualfolder"
        rebac_id_attr = "virtual_id"


class VirtualPost(Post):
    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "test/virtualpost"
        rebac_id_attr = "virtual_id"


class PropertyIdentityFolder(Folder):
    @property
    def public_identity(self) -> str:
        return f"property-{self.pk}"

    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "test/propertyfolder"
        rebac_id_attr = "public_identity"


class ColumnlessIdentityFolder(Folder):
    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "test/columnlessfolder"
        rebac_id_attr = "columnless_id"


class NonExpressionIdentityFolder(Folder):
    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "test/nonexpressionfolder"
        rebac_id_attr = "nonexpression_id"


class MissingLookupIdentityFolder(Folder):
    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "test/missinglookupfolder"
        rebac_id_attr = "missing_lookup_id"


class PrimarySluggedPost(SluggedPost):
    class Meta:
        proxy = True
        app_label = "testapp"
        rebac_resource_type = "test/primarysluggedpost"
        rebac_id_attr = "pk"


class SlugReference(RebacMixin, models.Model):
    virtual_id = VirtualEncodedIdentityField()
    target = models.ForeignKey(
        SluggedPost,
        to_field="slug",
        on_delete=models.CASCADE,
        related_name="references",
    )

    class Meta:
        app_label = "testapp"
        rebac_resource_type = "test/slugreference"
        rebac_id_attr = "virtual_id"
