"""Match reserved and wildcard holder terms against an actor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..actors import is_anonymous_actor
from ..types import SubjectRef
from .terms import AUTHENTICATED, Triple, anonymous


@dataclass(frozen=True)
class HolderClass:
    kind: Literal["authenticated", "anonymous", "wildcard"]
    type: str = ""


def class_of(triple: Triple) -> HolderClass | None:
    if triple == AUTHENTICATED:
        return HolderClass("authenticated")
    if triple == anonymous():
        return HolderClass("anonymous")
    type_, object_id, relation = triple
    if object_id == "*" and not relation:
        return HolderClass("wildcard", type_)
    return None


def matches(cls: HolderClass, actor: SubjectRef) -> bool:
    if cls.kind == "anonymous":
        return is_anonymous_actor(actor)
    if cls.kind == "authenticated":
        return not is_anonymous_actor(actor) and bool(actor.subject_id)
    return actor.subject_type == cls.type and not actor.optional_relation
