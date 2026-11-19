"""Rows for a policy's stand-in models, to any size, and the relationships that make its hard cases.

Everything is generated in the database, one ``INSERT … SELECT`` per table, from
the row number alone, so a build is deterministic and a table of ten million
rows costs what the database takes to write it. The same statements run on
PostgreSQL and on SQLite (which only the small sizes are meant for).

What decides a value:

- a key is the row number; a multi-table child takes a slice of its parent's;
- a foreign key names a row of its target chosen by a multiplicative hash of
  the row number, or by the policy's profile where locality matters (a
  message's parts sit next to the message);
- a self-reference is a tree eight wide;
- a generic key points at each model that declares a generic relation to it,
  in turn;
- a column a backing filters on takes the values the policy names for a share
  of the rows and another value for the rest: what a policy names is usually
  the special case (``public``, ``draft``), so it is rare unless the profile
  says otherwise (``TEXTS``, ``BOOLS``, ``COMMONLY_TRUE``);
- an optional generic key and an automatic many-to-many link are set on a
  share of the rows (``OPTIONAL``);
- a stored relation gets rows for a share of the objects (``DENSITY``), granted
  to background users and groups, and the cast's grants on top (``Cast``).
"""

from __future__ import annotations

import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import connections, models

from rebac.field_backing import (
    resolve_attribute_backing,
    resolve_const_backing,
    resolve_field_backing,
)
from rebac.models import active_relationship_model
from rebac.schema.ast import AttributeBinding, ConstBinding, FieldBinding, Schema
from rebac.schema.parser import parse_zed

MIX = 2654435761  # a multiplicative hash constant: consecutive rows land far apart
BRANCHING = 8  # children per node of a generated tree
SIZES = {"tiny": 4, "small": 40, "medium": 2_000, "large": 80_000}


@dataclass(frozen=True)
class Cast:
    """The users a benchmark reads as. Ids below ``background`` are reserved for them."""

    admin: int = 1  # member of every role the policy names as a constant
    heavy: int = 2  # direct grants on up to ``heavy_grants`` rows of each main type
    teams: int = 3  # in ``team_groups`` groups that hold grants on the main types
    light: int = 4  # in one group, with a handful of direct grants
    author: int = 5  # holds a grant on ``author_rows`` rows of each one-grant-per-row relation
    sparse: int = 6  # three grants, at the end of each main type's id range
    outsider: int = 7  # holds nothing
    background: int = 10
    heavy_grants: int = 25_000
    team_groups: int = 200
    author_rows: int = 100_000

    def members(self) -> dict[str, int]:
        return {
            name: getattr(self, name)
            for name in ("admin", "heavy", "teams", "light", "author", "sparse", "outsider")
        }


@dataclass
class Bench:
    """One exported policy over its stand-in models, sized."""

    skeleton: dict[str, Any]
    schema: Schema
    models: dict[str, type[models.Model]]
    profile: ModuleType
    unit: int
    using: str = "default"
    cast: Cast = field(default_factory=Cast)
    rows: dict[str, int] = field(default_factory=dict)
    offsets: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._samples: dict[tuple[Any, ...], Any] = {}
        self.labels = {model: label for label, model in self.models.items()}
        self.entries: dict[str, Any] = self.skeleton["models"]
        self.by_type = {
            entry["resource_type"]: label
            for label, entry in self.entries.items()
            if entry["resource_type"] and label in self.models
        }
        self.user = next(label for label, entry in self.entries.items() if entry["is_user"])
        self.user_type = self.entries[self.user]["resource_type"]
        self.vendor = connections[self.using].vendor
        self.optional: dict[tuple[str, str], int] = getattr(self.profile, "OPTIONAL", {})
        self.optional_default: int = getattr(self.profile, "DEFAULT_OPTIONAL", 15)
        self._size()
        self.filter_values, self.generic_targets = self._read_policy()

    # ---------- Sizes ----------

    def _size(self) -> None:
        weights: dict[str, float] = getattr(self.profile, "ROWS", {})
        default: float = getattr(self.profile, "DEFAULT_ROWS", 0.02)
        for label in self.models:
            managed = self.entries[label]["managed"]
            self.rows[label] = (
                max(3, int(weights.get(label, default) * self.unit)) if managed else 0
            )
        self.rows[self.user] = max(self.rows[self.user], self.cast.background + 10)
        children: dict[str, list[str]] = {}
        for label in self.models:
            for parent in self.entries[label]["parents"]:
                children.setdefault(parent, []).append(label)
        # A multi-table child is a slice of its parent: the parent holds every child and more.
        for parent in sorted(children, key=self._depth, reverse=True):
            offset = 0
            for child in sorted(children[parent]):
                self.offsets[child] = offset
                offset += self.rows[child]
            self.rows[parent] = max(self.rows[parent], offset + max(3, offset // 5))

    def _depth(self, label: str) -> int:
        parents = self.entries[label]["parents"]
        return 1 + max(map(self._depth, parents)) if parents else 0

    # ---------- What the policy says about columns ----------

    def _read_policy(
        self,
    ) -> tuple[dict[tuple[str, str], list[Any]], dict[tuple[str, str], list[str]]]:
        values: dict[tuple[str, str], list[Any]] = {}
        generic: dict[tuple[str, str], list[str]] = {}

        def column(model: type[models.Model], lookup: str, value: Any) -> None:
            current: Any = model
            parts = lookup.split("__")
            for index, part in enumerate(parts):
                try:
                    found = current._meta.get_field(part)
                except Exception:
                    return
                related = getattr(found, "related_model", None)
                if isinstance(related, type) and index < len(parts) - 1:
                    current = related
                    continue
                if isinstance(related, type) or current not in self.labels:
                    return
                suffix = parts[index + 1 :]
                if suffix and suffix != ["exact"]:
                    return  # isnull, in, a JSON key: the generic values serve
                seen = values.setdefault((self.labels[current], found.name), [])
                if value is not None and value not in seen:
                    seen.append(value)
                return

        for definition in self.schema.definitions:
            for relation in definition.relations:
                backing = relation.backing
                try:
                    if isinstance(backing, FieldBinding):
                        resolved = resolve_field_backing(definition, relation)
                        if resolved is None:
                            continue
                        for lookup, value in resolved.filters.items():
                            column(resolved.source_model, lookup, value)
                        if resolved.generic is not None and resolved.target_model in self.labels:
                            key = (self.labels[resolved.source_model], resolved.generic.name)
                            target = self.labels[resolved.target_model]
                            if target not in generic.setdefault(key, []):
                                generic[key].append(target)
                    elif isinstance(backing, ConstBinding):
                        constant = resolve_const_backing(definition, relation)
                        if constant is not None:
                            for lookup, value in constant.filters.items():
                                column(constant.source_model, lookup, value)
                    elif isinstance(backing, AttributeBinding):
                        attribute = resolve_attribute_backing(definition, relation)
                        if attribute is not None:
                            if attribute.resource is not None:
                                column(
                                    attribute.target_model, attribute.field.name, attribute.value
                                )
                            for lookup, value in attribute.filters.items():
                                column(attribute.target_model, lookup, value)
                except ValueError:
                    continue
        # Every model that declares a generic relation to a model is a target of its generic key.
        for label, entry in self.entries.items():
            for described in entry["fields"]:
                if described["kind"] != "generic_relation" or label not in self.models:
                    continue
                for candidate in self.entries[described["to"]]["fields"]:
                    if (
                        candidate["kind"] == "generic_key"
                        and candidate["ct_field"] == described["content_type_field"]
                        and candidate["fk_field"] == described["object_id_field"]
                    ):
                        targets = generic.setdefault((described["to"], candidate["name"]), [])
                        if label not in targets:
                            targets.append(label)
        return values, generic

    # ---------- SQL building blocks ----------

    def series(self, count: int) -> tuple[str, str]:
        """``(prefix, from clause)`` of a statement over the numbers ``1..count`` as ``g``."""
        if self.vendor == "postgresql":
            return "", f"FROM generate_series(1, {count}) AS s(g)"
        prefix = (
            f"WITH RECURSIVE s(g) AS (SELECT 1 UNION ALL SELECT g + 1 FROM s WHERE g < {count}) "
        )
        return prefix, "FROM s"

    @staticmethod
    def salt(*parts: str) -> int:
        return zlib.crc32(":".join(parts).encode()) % 1_000_003

    @staticmethod
    def percent(index: str, salt: int) -> str:
        """A number in ``0..99`` for a row, with no period a stride could fall in step with."""
        return f"((CAST({index} AS bigint) * {MIX} + {salt}) % 100)"

    @staticmethod
    def mix(index: str, salt: int, modulus: int) -> str:
        """A row number in ``1..modulus``, far from its neighbours'."""
        return f"(1 + ((CAST({index} AS bigint) * {MIX} + {salt}) % {max(modulus, 1)}))"

    def row_id(self, label: str, index: str) -> str:
        """The key of the ``index``-th row of ``label``."""
        parents = self.entries[label]["parents"]
        if parents:
            return self.row_id(parents[0], f"({self.offsets[label]} + {index})")
        return index

    def spread(self, label: str, target: str, index: str = "g") -> str:
        """The row of ``target`` a row of ``label`` sits under when children are laid out in order."""
        return f"(1 + ((CAST({index} AS bigint) - 1) * {self.rows[target]} / {max(self.rows[label], 1)}))"

    def foreign_key(self, label: str, name: str, index: str = "g") -> str | None:
        """The value of foreign key ``name`` on the ``index``-th row of ``label``; ``None`` for NULL."""
        override = getattr(self.profile, "FOREIGN_KEYS", {}).get((label, name))
        if override is not None:
            return override(self, index)
        model = self.models[label]
        found: Any = model._meta.get_field(name)
        target_model = found.related_model
        if target_model is ContentType:
            return None if found.null else str(self.content_type(label))
        target = self.labels[target_model]
        count = self.rows[target]
        inert = getattr(self.profile, "INERT", ())
        if count == 0 or (found.null and any(word in name for word in inert)):
            if not found.null:
                raise ValueError(f"{label}.{name} cannot be NULL and {target} has no rows")
            return None
        if found.unique or found.one_to_one:
            value = self._target_value(found, target, index)
            return f"CASE WHEN {index} <= {count} THEN {value} END" if found.null else value
        if target == label:
            parent = self._target_value(
                found, target, f"(({index} + {BRANCHING - 2}) / {BRANCHING})"
            )
            return f"CASE WHEN {index} > 1 THEN {parent} END"
        value = self._target_value(found, target, self.mix(index, self.salt(label, name), count))
        if found.null:
            some = self.percent(index, self.salt(label, name, "set"))
            return f"CASE WHEN {some} < 70 THEN {value} END"
        return value

    def _target_value(self, found: Any, target: str, index: str) -> str:
        column = found.target_field
        if column.primary_key:
            return self.row_id(target, index)
        return (
            f"('{column.name}-' || {index})"  # a unique text column, generated as its name and row
        )

    def content_type(self, label: str) -> int:
        return int(ContentType.objects.db_manager(self.using).get_for_model(self.models[label]).pk)

    # ---------- Rows a scenario reads ----------

    def actor(self, name: str) -> Any:
        """The cast member ``name`` as a subject."""
        from rebac import SubjectRef

        return SubjectRef.of(self.user_type, str(self.cast.members()[name]))

    def readable(self, label: str, actor: Any) -> Any:
        """The key of one row of ``label`` the actor may read (the lowest), or ``None``. Found once."""
        key = ("readable", label, actor)
        if key not in self._samples:
            rows = (
                self.models[label]
                .objects.with_actor(actor)
                .order_by("pk")
                .values_list("pk", flat=True)
            )
            self._samples[key] = next(iter(rows[:1]), None)
        return self._samples[key]

    def unreadable(self, label: str, actor: Any) -> Any:
        """The key of one row of ``label`` the actor may not read, or ``None``. Found once."""
        key = ("unreadable", label, actor)
        if key not in self._samples:
            from rebac.backends import backend

            candidates = self.sample(label, 40)
            resource_type = self.entries[label]["resource_type"]
            held = backend().held(
                subject=actor, action="read", resource_type=resource_type, ids=candidates
            )
            self._samples[key] = next(
                (int(value) for value in candidates if value not in held), None
            )
        return self._samples[key]

    def sample(self, label: str, count: int) -> list[str]:
        """``count`` keys of ``label`` scattered over its rows, as wire ids."""
        total = self.rows[label]
        rows = dict.fromkeys(1 + (n * MIX + 7) % total for n in range(min(count, total)))
        return [str(self.key(label, row)) for row in rows]

    def key(self, label: str, row: int) -> int:
        """The key of the ``row``-th row of ``label``."""
        parents = self.entries[label]["parents"]
        return self.key(parents[0], self.offsets[label] + row) if parents else row

    # ---------- Model rows ----------

    def columns(self, label: str) -> dict[str, str]:
        """Each column of ``label``'s table as an expression of the row number ``g``."""
        model = self.models[label]
        entry = self.entries[label]
        found: dict[str, str] = {}
        generic_columns: set[str] = set()
        for private in model._meta.private_fields:
            if not isinstance(private, GenericForeignKey) or private.model is not model:
                continue
            content_type: Any = model._meta.get_field(private.ct_field)
            object_id: Any = model._meta.get_field(private.fk_field)
            generic_columns |= {private.ct_field, private.fk_field}
            targets = [
                target
                for target in self.generic_targets.get((label, private.name), [])
                if self.rows.get(target)
            ]
            textual = object_id.get_internal_type() in ("CharField", "TextField")
            if not targets:
                if not content_type.null:
                    targets = [label]
                else:
                    # Nothing to point at: no type, and an id that names no row.
                    found[content_type.column] = "NULL"
                    found[object_id.column] = (
                        "NULL" if object_id.null else ("''" if textual else "0")
                    )
                    continue
            salt = self.salt(label, private.name)
            types = " ".join(
                f"WHEN {n} THEN {self.content_type(target)}" for n, target in enumerate(targets)
            )
            ids = " ".join(
                f"WHEN {n} THEN {self.row_id(target, self.mix('g', salt, self.rows[target]))}"
                for n, target in enumerate(targets)
            )
            kind = f"CASE (g % {len(targets)}) {types} END"
            value = f"CASE (g % {len(targets)}) {ids} END"
            if textual:
                value = f"CAST({value} AS text)"
            if content_type.null:
                # An optional generic key is set on a share of the rows.
                share = self.optional.get((label, private.name), self.optional_default)
                some = f"{self.percent('g', self.salt(label, private.name, 'set'))} < {share}"
                absent = "NULL" if object_id.null else ("''" if textual else "0")
                kind = f"CASE WHEN {some} THEN {kind} END"
                value = f"CASE WHEN {some} THEN {value} ELSE {absent} END"
            found[content_type.column] = kind
            found[object_id.column] = value
        bools: dict[tuple[str, str], int] = getattr(self.profile, "BOOLS", {})
        common: tuple[str, ...] = getattr(self.profile, "COMMONLY_TRUE", ())
        texts: dict[tuple[str, str], dict[str, int]] = getattr(self.profile, "TEXTS", {})
        column: Any
        for column in model._meta.local_concrete_fields:
            name = column.name
            if name in generic_columns:
                continue
            if column.primary_key:
                found[column.column] = self.row_id(label, "g") if entry["parents"] else "g"
            elif column.is_relation:
                found[column.column] = self.foreign_key(label, name) or "NULL"
            elif column.get_internal_type() == "BooleanField":
                share = bools.get((label, name), 95 if any(word in name for word in common) else 5)
                found[column.column] = f"({self.percent('g', self.salt(label, name))} < {share})"
            elif column.get_internal_type() in ("CharField", "TextField"):
                values = [
                    value
                    for value in self.filter_values.get((label, name), [])
                    if isinstance(value, str)
                ]
                if column.unique:
                    found[column.column] = f"('{name}-' || g)"
                elif values:
                    # The values the policy names share 30 rows in 100 unless the profile says.
                    shares = texts.get((label, name)) or {
                        value: 30 // len(values) for value in values
                    }
                    drawn = self.percent("g", self.salt(label, name))
                    upto, cases = 0, []
                    for value, portion in shares.items():
                        upto += portion
                        cases.append(f"WHEN {drawn} < {upto} THEN '{value}'")
                    found[column.column] = f"CASE {' '.join(cases)} ELSE 'other' END"
                else:
                    found[column.column] = "''"
            elif column.get_internal_type() in ("BigIntegerField", "IntegerField"):
                found[column.column] = "g"
            else:
                found[column.column] = "NULL"
        return found

    def statements(self) -> Iterator[tuple[str, str]]:
        """``(what, sql)`` for every model table and automatic many-to-many table."""
        quote = connections[self.using].ops.quote_name
        for label, model in self.models.items():
            count = self.rows[label]
            if not count:
                continue
            columns = self.columns(label)
            prefix, source = self.series(count)
            yield (
                f"{label}: {count}",
                f"{prefix}INSERT INTO {quote(model._meta.db_table)} "
                f"({', '.join(quote(name) for name in columns)}) "
                f"SELECT {', '.join(columns.values())} {source}",
            )
            for many in model._meta.local_many_to_many:
                through: Any = many.remote_field.through
                if not through._meta.auto_created:
                    continue
                target = self.labels[many.related_model]
                if not self.rows[target]:
                    continue
                near: Any = through._meta.get_field(many.m2m_field_name())
                far: Any = through._meta.get_field(many.m2m_reverse_field_name())
                salt = self.salt(label, many.name)
                share = self.optional.get((label, many.name), self.optional_default)
                yield (
                    f"{label}.{many.name}: {count}",
                    f"{prefix}INSERT INTO {quote(through._meta.db_table)} "
                    f"({quote(near.column)}, {quote(far.column)}) "
                    f"SELECT {self.row_id(label, 'g')}, "
                    f"{self.row_id(target, self.mix('g', salt, self.rows[target]))} {source} "
                    f"WHERE {self.percent('g', salt + 1)} < {share}",
                )

    # ---------- Relationship rows ----------

    def relationship_statements(self) -> Iterator[tuple[str, str]]:
        """``(what, sql)`` filling ``bench_tuples`` for every stored relation of the policy."""
        cast = self.cast
        users = self.rows[self.user]
        density: dict[tuple[str, str], int] = getattr(self.profile, "DENSITY", {})
        default_density: int = getattr(self.profile, "DEFAULT_DENSITY", 5)
        main: tuple[str, ...] = getattr(self.profile, "MAIN_TYPES", ())
        per_row: tuple[tuple[str, str], ...] = getattr(self.profile, "ONE_PER_ROW", ())
        sets = {
            (allowed.type, allowed.relation)
            for definition in self.schema.definitions
            for relation in definition.relations
            for allowed in relation.allowed_subjects
            if allowed.relation
        }
        named: dict[str, set[str]] = {}
        for definition in self.schema.definitions:
            for relation in definition.relations:
                if isinstance(relation.backing, ConstBinding) and relation.allowed_subjects:
                    named.setdefault(relation.allowed_subjects[0].type, set()).add(
                        relation.backing.target_id
                    )
                for allowed in relation.allowed_subjects:
                    if allowed.id and allowed.id != "*":
                        named.setdefault(allowed.type, set()).add(allowed.id)

        def background_user(index: str, salt: int) -> str:
            return f"({cast.background} + {self.mix(index, salt, users - cast.background)})"

        def insert(resource: str, subject: str, relation: str, subject_type: str, subject_relation: str,
                   count: int, where: str = "") -> str:  # fmt: skip
            prefix, source = self.series(count)
            return (
                f"{prefix}INSERT INTO bench_tuples "
                f"SELECT '{resource_type}', CAST({resource} AS text), '{relation}', "
                f"'{subject_type}', CAST({subject} AS text), '{subject_relation}' {source}"
                + (f" WHERE {where}" if where else "")
            )

        for definition in self.schema.definitions:
            resource_type = definition.resource_type
            label = self.by_type.get(resource_type)
            count = self.rows.get(label, 0) if label else 0
            for relation in definition.relations:
                if relation.backing is not None:
                    continue
                key = (resource_type, relation.name)
                what = f"{resource_type}#{relation.name}"
                percent = density.get(key, default_density)
                membership = key in sets
                for allowed in relation.allowed_subjects:
                    if allowed.with_caveat:
                        continue
                    salt = self.salt(what, allowed.type, allowed.relation)
                    subject_label = self.by_type.get(allowed.type)
                    subject_rows = self.rows.get(subject_label, 0) if subject_label else 0
                    plain_user = allowed.type == self.user_type and not allowed.relation
                    if not count:
                        # Objects that exist only by name: a role. The administrator holds each.
                        if plain_user and not allowed.wildcard:
                            for name in sorted(named.get(resource_type, ())):
                                yield (
                                    what,
                                    insert(
                                        f"'{name}'",
                                        str(cast.admin),
                                        relation.name,
                                        allowed.type,
                                        "",
                                        1,
                                    ),
                                )
                        continue
                    assert label is not None
                    resource = self.row_id(label, "g")
                    chosen = f"(CAST(g AS bigint) * {MIX} + {salt}) % 100 < {percent}"
                    if allowed.wildcard:
                        if plain_user:
                            yield what, insert(resource, "'*'", relation.name, allowed.type, "", count,
                                               f"(CAST(g AS bigint) * {MIX} + {salt}) % 100 < 1")  # fmt: skip
                        continue
                    if allowed.id:
                        yield what, insert(resource, f"'{allowed.id}'", relation.name, allowed.type,
                                           allowed.relation, count, chosen)  # fmt: skip
                        continue
                    if plain_user and membership:
                        # A membership is generated from the user's side: everyone is in two.
                        for turn in (0, 1):
                            member = f"({cast.background} + g)"
                            group = self.row_id(label, self.mix("g", salt + turn, count))
                            yield what, insert(group, member, relation.name, allowed.type, "",
                                               users - cast.background)  # fmt: skip
                        groups = min(cast.team_groups, count)
                        yield (
                            what,
                            insert(
                                self.row_id(label, "g"),
                                str(cast.teams),
                                relation.name,
                                allowed.type,
                                "",
                                groups,
                            ),
                        )
                        yield (
                            what,
                            insert(
                                self.row_id(label, "g"),
                                str(cast.light),
                                relation.name,
                                allowed.type,
                                "",
                                1,
                            ),
                        )
                        yield (
                            what,
                            insert(
                                self.row_id(label, "g"),
                                str(cast.heavy),
                                relation.name,
                                allowed.type,
                                "",
                                min(3, count),
                            ),
                        )
                        continue
                    if plain_user:
                        if key in per_row:
                            yield what, insert(resource, background_user("g", salt), relation.name,
                                               allowed.type, "", count, f"g > {cast.author_rows}")  # fmt: skip
                            yield what, insert(resource, str(cast.author), relation.name, allowed.type, "",
                                               min(count, cast.author_rows))  # fmt: skip
                            continue
                        yield (
                            what,
                            insert(
                                resource,
                                background_user("g", salt),
                                relation.name,
                                allowed.type,
                                "",
                                count,
                                chosen,
                            ),
                        )
                        if resource_type in main:
                            stride = max(2, count // cast.heavy_grants)
                            yield (
                                what,
                                insert(
                                    resource,
                                    str(cast.heavy),
                                    relation.name,
                                    allowed.type,
                                    "",
                                    count,
                                    f"g % {stride} = 0",
                                ),
                            )
                            yield (
                                what,
                                insert(
                                    resource,
                                    str(cast.sparse),
                                    relation.name,
                                    allowed.type,
                                    "",
                                    count,
                                    f"g > {count - 3}",
                                ),
                            )
                            yield (
                                what,
                                insert(
                                    resource,
                                    str(cast.light),
                                    relation.name,
                                    allowed.type,
                                    "",
                                    min(5, count),
                                ),
                            )
                        continue
                    if not subject_rows:
                        continue
                    assert subject_label is not None
                    subject = self.row_id(subject_label, self.mix("g", salt, subject_rows))
                    share = max(1, percent // 2)
                    yield what, insert(resource, subject, relation.name, allowed.type, allowed.relation, count,
                                       f"(CAST(g AS bigint) * {MIX} + {salt}) % 100 < {share}")  # fmt: skip
                    if (
                        resource_type in main
                        and (allowed.type, allowed.relation) in sets
                        and allowed.relation
                    ):
                        groups = min(cast.team_groups, subject_rows)
                        stride = max(2, count // cast.heavy_grants)
                        team = self.row_id(subject_label, f"(1 + (g / {stride}) % {groups})")
                        yield what, insert(resource, team, relation.name, allowed.type, allowed.relation, count,
                                           f"g % {stride} = 1")  # fmt: skip


def load(name: str, size: str | int, *, using: str = "default") -> Bench:
    """The bench for exported policy ``name`` at ``size`` (a name in ``SIZES`` or a unit count)."""
    import importlib

    from tests.benchapp import models as standins

    profile = importlib.import_module(f"tests.bench.policies.{name}.profile")
    unit = SIZES[size] if isinstance(size, str) and size in SIZES else int(size)
    return Bench(
        skeleton=standins.SKELETON,
        schema=parse_zed(standins.POLICY),
        models=standins.MODELS,
        profile=profile,
        unit=unit,
        using=using,
    )


def build(bench: Bench, *, echo: bool = False) -> dict[str, int]:
    """Fill the stand-in tables and the relationship table of an empty database."""
    connection = connections[bench.using]
    quote = connection.ops.quote_name
    written: dict[str, int] = {}
    with connection.cursor() as cursor:
        replica = False
        if bench.vendor == "postgresql":
            try:
                # No foreign-key trigger fires: every generated reference is to a generated row.
                cursor.execute("SET session_replication_role = replica")
                replica = True
            except Exception:
                replica = False
        if not replica:
            connection.disable_constraint_checking()
        try:
            for what, sql in bench.statements():
                cursor.execute(sql)
                written[what.split(":")[0]] = cursor.rowcount
                if echo:
                    print(f"  {what}", flush=True)
            cursor.execute(
                "CREATE TEMPORARY TABLE bench_tuples (resource_type text, resource_id text, "
                "relation text, subject_type text, subject_id text, subject_relation text)"
            )
            for _what, sql in bench.relationship_statements():
                cursor.execute(sql)
            model = active_relationship_model()
            table = quote(model._meta.db_table)
            if hasattr(model, "resource_fk"):
                from rebac.models import RebacResource

                registry = quote(RebacResource._meta.db_table)
                cursor.execute(
                    f"INSERT INTO {registry} (resource_type, resource_id, object_pk) "
                    "SELECT resource_type, resource_id, '' FROM ("
                    "SELECT resource_type, resource_id FROM bench_tuples "
                    "UNION SELECT subject_type, subject_id FROM bench_tuples) AS refs"
                )
                cursor.execute(
                    f"INSERT INTO {table} (resource_fk_id, relation, subject_fk_id, "
                    "optional_subject_relation, caveat_name, caveat_key, written_at_xid) "
                    "SELECT DISTINCT r.id, t.relation, s.id, t.subject_relation, '', '', 0 "
                    f"FROM bench_tuples t JOIN {registry} r "
                    "ON r.resource_type = t.resource_type AND r.resource_id = t.resource_id "
                    f"JOIN {registry} s ON s.resource_type = t.subject_type AND s.resource_id = t.subject_id"
                )
            else:
                cursor.execute(
                    f"INSERT INTO {table} (resource_type, resource_id, relation, subject_type, "
                    "subject_id, optional_subject_relation, caveat_name, caveat_key, written_at_xid) "
                    "SELECT DISTINCT resource_type, resource_id, relation, subject_type, subject_id, "
                    "subject_relation, '', '', 0 FROM bench_tuples"
                )
            written["relationships"] = cursor.rowcount
            cursor.execute("DROP TABLE bench_tuples")
        finally:
            if replica:
                cursor.execute("SET session_replication_role = DEFAULT")
            else:
                connection.enable_constraint_checking()
        if bench.vendor == "postgresql":
            cursor.execute("ANALYZE")
    return written
