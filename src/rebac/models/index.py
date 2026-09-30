"""Internal storage for the derived permission index (not a public API)."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from django.db import models

from rebac.index.time import active_q, time_max


class IndexQuerySet[M: models.Model](models.QuerySet[M]):
    """Small alias-preserving predicates shared by internal index managers."""

    def active(self, now: datetime | models.Expression) -> Self:
        if not hasattr(self.model, "expires_at"):
            return self.all()
        return self.filter(active_q(now))

    def unconditional(self) -> Self:
        if not hasattr(self.model, "condition"):
            return self.all()
        return self.filter(condition__isnull=True)


class IndexManager(models.Manager.from_queryset(IndexQuerySet)):  # type: ignore[misc]
    """All methods remain available after .using(alias)."""


class IndexTerm(models.Model):
    type = models.CharField(max_length=64)
    object_id = models.CharField(max_length=64)
    relation = models.CharField(max_length=64, default="")
    objects = IndexManager()

    class Meta:
        db_table = "rebac_term"
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["type", "object_id", "relation"], name="rebac_term_uniq"
            )
        ]


class IndexPayload(models.Model):
    expires_at = models.DateTimeField(default=time_max)
    condition = models.JSONField(null=True)
    condition_key = models.CharField(max_length=64, default="")
    objects = IndexManager()

    class Meta:
        abstract = True
        default_permissions = ()


class IndexEdge(IndexPayload):
    resource = models.ForeignKey(IndexTerm, on_delete=models.DO_NOTHING, related_name="edges_out")
    resource_type = models.CharField(max_length=64)
    relation = models.CharField(max_length=64)
    subject = models.ForeignKey(
        IndexTerm, on_delete=models.DO_NOTHING, related_name="edges_as_subject"
    )
    target = models.ForeignKey(IndexTerm, on_delete=models.DO_NOTHING, related_name="edges_in")
    source = models.CharField(
        max_length=9, choices=[(v, v) for v in ("tuple", "field", "attribute", "const")]
    )

    class Meta(IndexPayload.Meta):
        abstract = False
        db_table = "rebac_edge"
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "relation", "subject", "source", "condition_key"],
                name="rebac_edge_uniq",
            )
        ]
        indexes = [
            models.Index(fields=["resource_type", "relation"], name="rebac_edge_node_idx"),
            models.Index(fields=["target", "relation"], name="rebac_edge_target_idx"),
        ]  # The subject FK supplies the single-column subject index.


class IndexMember(IndexPayload):
    member = models.ForeignKey(IndexTerm, on_delete=models.DO_NOTHING, related_name="memberships")
    member_type = models.CharField(max_length=64)
    set = models.ForeignKey(IndexTerm, on_delete=models.DO_NOTHING, related_name="members")
    pass_id = models.BigIntegerField(null=True)
    round = models.PositiveIntegerField(null=True)

    class Meta(IndexPayload.Meta):
        abstract = False
        db_table = "rebac_membership"
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=["member", "set", "condition_key"], name="rebac_member_uniq"
            )
        ]
        indexes = [
            models.Index(fields=["set", "member"], name="rebac_member_set_idx"),
            models.Index(fields=["pass_id", "round"], name="rebac_member_pass_idx"),
        ]


class IndexCover(IndexPayload):
    scope = models.ForeignKey(IndexTerm, on_delete=models.DO_NOTHING, related_name="covers")
    resource_type = models.CharField(max_length=64)
    node = models.CharField(max_length=64)
    holder = models.ForeignKey(IndexTerm, on_delete=models.DO_NOTHING, related_name="held_covers")
    site = models.CharField(max_length=64, default="")
    pass_id = models.BigIntegerField(null=True)
    round = models.PositiveIntegerField(null=True)

    class Meta(IndexPayload.Meta):
        abstract = False
        db_table = "rebac_grant"
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "scope",
                    "node",
                    "holder",
                    "site",
                    "condition_key",
                ],
                name="rebac_cover_uniq",
            )
        ]
        indexes = [
            models.Index(
                fields=["resource_type", "node", "site", "holder"], name="rebac_grant_node_idx"
            ),
            models.Index(fields=["scope", "node"], name="rebac_cover_scope_idx"),
            models.Index(fields=["holder", "site", "node"], name="rebac_grant_holder_idx"),
            models.Index(fields=["pass_id", "round"], name="rebac_grant_pass_idx"),
        ]


class IndexWork(models.Model):
    pass_id = models.BigIntegerField()
    kind = models.CharField(max_length=64)
    term = models.ForeignKey(IndexTerm, null=True, on_delete=models.DO_NOTHING, related_name="+")
    node = models.CharField(max_length=64, default="")
    phase = models.CharField(max_length=6, choices=[(v, v) for v in ("old", "new", "region")])
    objects = IndexManager()

    class Meta:
        db_table = "rebac_index_work"
        default_permissions = ()
        indexes = [
            models.Index(fields=["pass_id", "phase", "kind"], name="rebac_work_pass_phase_idx"),
            models.Index(fields=["pass_id", "phase", "term"], name="rebac_work_pass_term_idx"),
        ]


class IndexState(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    objects = IndexManager()

    class Meta:
        db_table = "rebac_index_state"
        default_permissions = ()
