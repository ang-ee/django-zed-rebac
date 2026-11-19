"""Policies shaped like an application's, for the switchable read mechanisms (proposal 0020).

Flat groups, roles that admit groups, a constant administrator arm, grants to
users, groups and everyone, parents through foreign keys, and exclusions through
a filtered constant: every part is one the walk and the row scope read whole,
so a test over them exercises the mechanisms and not their fallback.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from rebac import ObjectRef, RelationshipTuple, SubjectRef
from tests.testapp.models import ScopeThread

MEMBER = SubjectRef.of("auth/user", "member")
ADMIN = SubjectRef.of("auth/user", "admin")
EVERYONE = SubjectRef.of("auth/user", "*")

FIXED = """
definition auth/user {}
definition auth/group { relation member: auth/user }
definition scope/role { relation member: auth/user | auth/group#member }
definition scope/thread {
    relation admin: scope/role // rebac:const=admin
    relation reader: auth/user | auth/group#member | auth/user:*
    relation team: scope/role#member
    relation blocked: auth/user | auth/group#member
    relation hidden: scope/thread // rebac:const={"target_id":"hidden","filters":{"label":"hidden"}}
    permission conceal = authenticated
    permission withheld = hidden->conceal
    permission granted = reader + team + admin->member
    permission read = (granted - withheld) - blocked
    permission both = reader & team
}
definition scope/message {
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user
    permission read = thread->read + direct
}
definition scope/part {
    relation message: scope/message // rebac:field=message
    relation first: scope/message // rebac:field={"path":"message","filters":{"fragment_id":1}}
    permission read = message->read
    permission opening = first->read
}
"""


def seeds(count: int, fast: int = 6) -> list[Any]:
    """``count`` seeds: the first ``fast`` run in the default suite, the rest in the slow tier.

    A random-policy case reads every model as two actors under each setting,
    a third of a second apiece; the default suite keeps both generators and
    both storage layouts, and the slow tier the breadth.
    """
    return [
        seed if seed < fast else pytest.param(seed, marks=pytest.mark.slow) for seed in range(count)
    ]


def group(name: str) -> SubjectRef:
    return SubjectRef.of("auth/group", name, "member")


def role(name: str) -> SubjectRef:
    return SubjectRef.of("scope/role", name, "member")


def grant(
    type_: str, id_: object, relation: str, subject: SubjectRef = MEMBER
) -> RelationshipTuple:
    return RelationshipTuple(ObjectRef(type_, str(id_)), relation, subject)


def application_shaped(seed: int) -> tuple[str, list[RelationshipTuple], dict[int, str]]:
    """``(schema, tuples, thread labels)`` for one seed."""
    rng = random.Random(seed)

    def union(atoms: list[str]) -> str:
        return " + ".join(rng.sample(atoms, rng.randint(1, len(atoms))))

    def shaped(grants: list[str], denials: list[str]) -> str:
        shape = rng.choice(["union", "minus", "kept", "both", "nested"])
        if shape == "union":
            return union(grants)
        if shape == "minus":
            return f"({union(grants)}) - ({union(denials)})"
        if shape == "kept":
            return f"(({union(grants)}) - ({union(denials)})) + ({union(grants)})"
        if shape == "both":
            return f"({union(grants)}) & ({union(grants)})"
        return f"({union(grants)}) - (({union(denials)}) - ({union(grants)}))"

    thread_grants = ["reader", "editor", "team", "admin->member"]
    thread_denials = ["blocked", "withheld"]
    schema = f"""
definition auth/user {{}}
definition auth/group {{ relation member: auth/user }}
definition scope/role {{ relation member: auth/user | auth/group#member }}
definition scope/thread {{
    relation admin: scope/role // rebac:const=admin
    relation reader: auth/user | auth/group#member | auth/user:*
    relation editor: auth/user | auth/group#member
    relation team: scope/role#member
    relation blocked: auth/user | auth/group#member
    relation hidden: scope/thread // rebac:const={{"target_id":"hidden","filters":{{"label":"hidden"}}}}
    permission conceal = authenticated
    permission withheld = hidden->conceal
    permission write = {shaped(thread_grants, thread_denials)}
    permission read = {shaped([*thread_grants, "write"], thread_denials)}
}}
definition scope/message {{
    relation admin: scope/role // rebac:const=admin
    relation thread: scope/thread // rebac:field=thread
    relation direct: auth/user | auth/group#member
    relation banned: auth/user
    permission read = {shaped(["thread->read", "thread->write", "direct", "admin->member"], ["banned", "thread->withheld"])}
}}
definition scope/part {{
    relation message: scope/message // rebac:field=message
    relation first: scope/message // rebac:field={{"path":"message","filters":{{"fragment_id":1}}}}
    relation direct: auth/user
    permission read = {shaped(["message->read", "first->read", "direct"], ["first->read", "direct"])}
}}
"""
    candidates = [
        grant("auth/group", "g1", "member"),
        grant("auth/group", "g1", "member", ADMIN),
        grant("auth/group", "g2", "member"),
        grant("scope/role", "admin", "member", ADMIN),
        grant("scope/role", "admin", "member", group("g2")),
        grant("scope/role", "team", "member"),
        grant("scope/role", "team", "member", group("g1")),
        *(grant("scope/thread", n, "reader") for n in (1, 2, 3)),
        *(grant("scope/thread", n, "reader", group("g1")) for n in (1, 2)),
        grant("scope/thread", 2, "reader", EVERYONE),
        *(grant("scope/thread", n, "editor") for n in (1, 2)),
        grant("scope/thread", 1, "editor", group("g2")),
        *(grant("scope/thread", n, "team", role("team")) for n in (1, 2)),
        *(grant("scope/thread", n, "blocked") for n in (1, 2)),
        grant("scope/thread", 2, "blocked", group("g1")),
        *(grant("scope/message", n, "direct") for n in (1, 2, 3)),
        grant("scope/message", 3, "direct", group("g2")),
        *(grant("scope/message", n, "banned") for n in (1, 3)),
        *(grant("scope/part", n, "direct") for n in (1, 2, 3)),
    ]
    tuples = [item for item in candidates if rng.random() < 0.4]
    labels = {n: rng.choice(["thread", "thread", "hidden"]) for n in (1, 2)}
    return schema, tuples, labels


def label_threads(labels: dict[int, str]) -> None:
    for pk, label in labels.items():
        ScopeThread._base_manager.filter(pk=pk).update(label=label)
