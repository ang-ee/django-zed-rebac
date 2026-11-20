"""How the Angee policy's stand-in tables are filled, and how an application reads them.

The policy and skeleton beside this file are ``rebac.skeleton.export`` run on a
composed host of ang-ee/angee-django (the commit is in ``skeleton.json``).
"""

from __future__ import annotations

from typing import Any

# Rows per unit of size. A unit is roughly one user's worth of data.
ROWS = {
    "iam.User": 1,
    "iam.Group": 0.2,
    "spaces.Group": 0.05,
    "spaces.Membership": 1,
    "parties.Party": 3,
    "parties.Person": 1.5,
    "parties.Organization": 0.5,
    "parties.Handle": 4,
    "messaging.Channel": 0.02,
    "messaging.Thread": 4,
    "messaging.Message": 20,
    "messaging.Part": 30,
    "messaging.Participant": 40,
    "messaging.Reaction": 5,
    "messaging.ThreadAttachment": 2,
    "storage.Drive": 0.05,
    "storage.Folder": 2,
    "storage.File": 15,
    "storage.FileAttachment": 6,
    "projects.Project": 0.2,
    "projects.Task": 6,
    "knowledge.Page": 2,
    "tags.TagAssignment": 6,
    "provenance.Changeset": 40,
}
DEFAULT_ROWS = 0.02

# Nullable foreign keys that stay NULL: they mark a rare state, not a parent.
INERT = (
    "trash", "merged", "dismiss", "retire", "acknowledg", "snooz", "converted", "created_from",
    "repeated", "clarification", "introduced", "delegate", "release", "current_", "concrete_type",
    "discovery",
)  # fmt: skip
# Flags that are true for nineteen rows in twenty; every other flag is true for one.
COMMONLY_TRUE = ("active", "confirmed")
# Of the values the policy names in a filter, the share of rows that carry each.
TEXTS = {
    ("iam.User", "kind"): {"person": 95},
    ("storage.File", "visibility"): {"inherited": 80, "record": 15},
    ("storage.File", "upload_state"): {"draft": 3},
    ("projects.Task", "visibility"): {"inherited": 85},
    ("messaging.ThreadAttachment", "role"): {"chatter": 60, "source": 30},
    ("calendar.CalendarEvent", "visibility"): {"default": 90, "public": 3},
    ("spaces.Group", "visibility"): {"public": 3},
    ("spaces.Membership", "role"): {"member": 80, "moderator": 8, "owner": 4, "viewer": 8},
    ("proposals.Round", "status"): {"opened": 30},
    ("proposals.Proposal", "state"): {"draft": 20},
}

# The types the cast holds grants on.
MAIN_TYPES = (
    "messaging/thread", "storage/file", "storage/drive", "storage/folder", "projects/project",
    "projects/task", "knowledge/page", "parties/party",
)  # fmt: skip
# Relations with one grant for every row: the framework writes one per changeset.
ONE_PER_ROW = (("provenance/changeset", "actor"),)
# The relationship ``read --uncommitted`` writes for the actor, on an object that does not exist.
UNCOMMITTED_GRANT = ("messaging/thread", "reader")
# The types a reader sees as labels where a row it reads refers to them (``rebac.display``,
# proposal 0024), and the fields a label shows. The stand-ins carry only the columns the policy
# reads, no names: a label is the key, and a file's visibility.
DISPLAY_FIELDS = {
    "messaging.Thread": ("id",),
    "parties.Party": ("id",),
    "storage.File": ("visibility",),
}


def _under(child: str, parent: str) -> Any:
    return lambda bench, g: bench.row_id(parent, bench.spread(child, parent, g))


FOREIGN_KEYS = {
    # A thread's messages, a message's parts and people, sit next to each other.
    ("messaging.Message", "thread"): _under("messaging.Message", "messaging.Thread"),
    ("messaging.Message", "channel"): lambda bench, g: bench.foreign_key(
        "messaging.Thread", "channel", bench.spread("messaging.Message", "messaging.Thread", g)
    ),
    ("messaging.Part", "message"): _under("messaging.Part", "messaging.Message"),
    ("messaging.Participant", "message"): _under("messaging.Participant", "messaging.Message"),
    ("messaging.Participant", "thread"): lambda bench, g: bench.row_id(
        "messaging.Thread",
        bench.spread(
            "messaging.Message",
            "messaging.Thread",
            bench.spread("messaging.Participant", "messaging.Message", g),
        ),
    ),
    ("messaging.Reaction", "message"): _under("messaging.Reaction", "messaging.Message"),
    ("parties.Handle", "party"): _under("parties.Handle", "parties.Party"),
    ("projects.Task", "project"): _under("projects.Task", "projects.Project"),
}


# ---------- Reads, as the application makes them ----------
#
# Each takes the bench and an actor and returns what it read, as something two
# runs can compare. Relations are loaded as the framework's GraphQL layer loads
# them: one scoped prefetch per relation.


def inbox(bench: Any, actor: Any) -> Any:
    """The first page of threads, newest first, with each thread's messages and their parts."""
    threads = (
        bench.models["messaging.Thread"]
        .objects.with_actor(actor)
        .order_by("-pk")
        .rebac_prefetch_related("messages__parts")[:25]
    )
    return [(t.pk, sorted((m.pk, len(m.parts.all())) for m in t.messages.all())) for t in threads]


def inbox_messages(bench: Any, actor: Any) -> Any:
    """The newest messages of the actor's inbox (``read_inbox``), each with its parts, people,
    reactions and stars, which are read under ``read`` through ``message->read``."""
    messages = (
        bench.models["messaging.Message"]
        .objects.with_actor(actor)
        .with_action("read_inbox")
        .order_by("-pk")
        .rebac_prefetch_related("parts", "participants", "reactions", "stars")[:25]
    )
    return [
        (
            m.pk,
            sorted(p.pk for p in m.parts.all()),
            sorted(p.pk for p in m.participants.all()),
            sorted(r.pk for r in m.reactions.all()),
            sorted(s.pk for s in m.stars.all()),
        )
        for m in messages
    ]


def selected(bench: Any, actor: Any) -> Any:
    """One message with what a reader shows: its thread, sender, parts and files, people, reactions."""
    pk = bench.readable("messaging.Message", actor)
    messages = (
        bench.models["messaging.Message"]
        .objects.with_actor(actor)
        .filter(pk=pk)
        .rebac_prefetch_related(
            "thread", "sender__party", "parts__file", "participants__handle__party", "reactions"
        )
    )
    return [
        (
            m.pk,
            m.thread_id,
            sorted((p.pk, p.file_id) for p in m.parts.all()),
            sorted((p.pk, p.handle_id) for p in m.participants.all()),
            sorted(r.pk for r in m.reactions.all()),
        )
        for m in messages
    ]


def transcript(bench: Any, actor: Any) -> Any:
    """A thread's messages in order, with their parts and the people on them."""
    thread = bench.readable("messaging.Thread", actor)
    messages = (
        bench.models["messaging.Message"]
        .objects.with_actor(actor)
        .filter(thread_id=thread)
        .order_by("pk")
        .rebac_prefetch_related("parts", "participants__handle")[:50]
    )
    return [
        (m.pk, sorted(p.pk for p in m.parts.all()), sorted(p.pk for p in m.participants.all()))
        for m in messages
    ]


def _label(row: Any) -> Any:
    """A label as a read delivered it: its key and whether it is a display row, or None."""
    from rebac import display

    return None if row is None else (row.pk, display.is_display_row(row))


def _labelled(bench: Any, actor: Any, label: str) -> Any:
    """The queryset of a label relation's prefetch: it asks for display rows, a request that
    changes nothing unless ``REBAC_DISPLAY_FIELDS`` is on (the ``labels`` mechanism)."""
    return bench.models[label].objects.with_actor(actor).for_display()


def labels(bench: Any, actor: Any) -> Any:
    """One message with the labels a reader shows: its thread, its sender's party, its files."""
    from django.db.models import Prefetch

    pk = bench.readable("messaging.Message", actor)
    models = bench.models
    messages = (
        models["messaging.Message"]
        .objects.with_actor(actor)
        .filter(pk=pk)
        .prefetch_related(
            Prefetch("thread", queryset=_labelled(bench, actor, "messaging.Thread")),
            Prefetch("sender", queryset=models["parties.Handle"].objects.with_actor(actor)),
            Prefetch("sender__party", queryset=_labelled(bench, actor, "parties.Party")),
            Prefetch("parts", queryset=models["messaging.Part"].objects.with_actor(actor)),
            Prefetch("parts__file", queryset=_labelled(bench, actor, "storage.File")),
        )
    )
    return [
        (
            m.pk,
            _label(m.thread),
            _label(m.sender and m.sender.party),
            sorted((p.pk, _label(p.file)) for p in m.parts.all()),
        )
        for m in messages
    ]


def page_labels(bench: Any, actor: Any) -> Any:
    """The newest 50 messages of the actor's first thread, each with its thread and sender's party."""
    from django.db.models import Prefetch

    thread = bench.readable("messaging.Thread", actor)
    models = bench.models
    messages = (
        models["messaging.Message"]
        .objects.with_actor(actor)
        .filter(thread_id=thread)
        .order_by("-pk")
        .prefetch_related(
            Prefetch("thread", queryset=_labelled(bench, actor, "messaging.Thread")),
            Prefetch("sender", queryset=models["parties.Handle"].objects.with_actor(actor)),
            Prefetch("sender__party", queryset=_labelled(bench, actor, "parties.Party")),
        )[:50]
    )
    return [(m.pk, _label(m.thread), _label(m.sender and m.sender.party)) for m in messages]


def inbox_labels(bench: Any, actor: Any) -> Any:
    """The newest messages of the actor's inbox (``read_inbox``) as a list shows them: each with
    its thread and its sender's party as labels, its parts (read under ``read`` through
    ``message->read``) with their files as labels, and its participants."""
    from django.db.models import Prefetch

    models = bench.models
    messages = (
        models["messaging.Message"]
        .objects.with_actor(actor)
        .with_action("read_inbox")
        .order_by("-pk")
        .prefetch_related(
            Prefetch("thread", queryset=_labelled(bench, actor, "messaging.Thread")),
            Prefetch("sender", queryset=models["parties.Handle"].objects.with_actor(actor)),
            Prefetch("sender__party", queryset=_labelled(bench, actor, "parties.Party")),
            Prefetch("parts", queryset=models["messaging.Part"].objects.with_actor(actor)),
            Prefetch("parts__file", queryset=_labelled(bench, actor, "storage.File")),
            Prefetch(
                "participants", queryset=models["messaging.Participant"].objects.with_actor(actor)
            ),
        )[:25]
    )
    return [
        (
            m.pk,
            _label(m.thread),
            _label(m.sender and m.sender.party),
            sorted((p.pk, _label(p.file)) for p in m.parts.all()),
            sorted(p.pk for p in m.participants.all()),
        )
        for m in messages
    ]


def files(bench: Any, actor: Any) -> Any:
    """The newest files the actor may read: a page of a large table."""
    rows = bench.models["storage.File"].objects.with_actor(actor).order_by("-pk")[:50]
    return [f.pk for f in rows]


def folder(bench: Any, actor: Any) -> Any:
    """The files of one folder: a read through a tree."""
    pk = bench.readable("storage.Folder", actor)
    rows = (
        bench.models["storage.File"]
        .objects.with_actor(actor)
        .filter(folder_id=pk)
        .order_by("pk")[:50]
    )
    return [f.pk for f in rows]


def tasks(bench: Any, actor: Any) -> Any:
    """A project's tasks with their subtasks."""
    pk = bench.readable("projects.Project", actor)
    rows = (
        bench.models["projects.Task"]
        .objects.with_actor(actor)
        .filter(project_id=pk)
        .order_by("pk")
        .rebac_prefetch_related("children")[:50]
    )
    return [(t.pk, sorted(c.pk for c in t.children.all())) for t in rows]


def people(bench: Any, actor: Any) -> Any:
    """A page of parties with their handles."""
    rows = (
        bench.models["parties.Party"]
        .objects.with_actor(actor)
        .order_by("pk")
        .rebac_prefetch_related("handles")[:50]
    )
    return [(p.pk, sorted(h.pk for h in p.handles.all())) for p in rows]


def changes(bench: Any, actor: Any) -> Any:
    """The actor's latest changesets: the relation with one grant for every row."""
    rows = bench.models["provenance.Changeset"].objects.with_actor(actor).order_by("-pk")[:50]
    return [c.pk for c in rows]


def held_files(bench: Any, actor: Any) -> Any:
    """Which of a hundred files the actor may read: a bounded check."""
    from rebac.backends import backend

    ids = bench.sample("storage.File", 100)
    return sorted(
        backend().held(subject=actor, action="read", resource_type="storage/file", ids=ids)
    )


def held_messages(bench: Any, actor: Any) -> Any:
    """Which of a hundred messages the actor may read."""
    from rebac.backends import backend

    ids = bench.sample("messaging.Message", 100)
    return sorted(
        backend().held(subject=actor, action="read", resource_type="messaging/message", ids=ids)
    )


def point(bench: Any, actor: Any) -> Any:
    """Two single checks: a message the actor may read, and one it may not."""
    from rebac import ObjectRef
    from rebac.backends import backend

    readable = bench.readable("messaging.Message", actor)
    hidden = bench.unreadable("messaging.Message", actor)
    local = backend()
    return [
        local.check_access(
            subject=actor, action="read", resource=ObjectRef("messaging/message", str(pk))
        ).allowed
        for pk in (readable, hidden)
        if pk is not None
    ]


SCENARIOS = {
    "inbox": inbox,
    "inbox_messages": inbox_messages,
    "selected": selected,
    "labels": labels,
    "page_labels": page_labels,
    "inbox_labels": inbox_labels,
    "transcript": transcript,
    "files": files,
    "folder": folder,
    "tasks": tasks,
    "people": people,
    "changes": changes,
    "held_files": held_files,
    "held_messages": held_messages,
    "point": point,
}
