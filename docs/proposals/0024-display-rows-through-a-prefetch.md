# 0024: Display rows through a prefetch

Status: **proposed**, as an extension of the experimental `REBAC_DISPLAY_FIELDS` (proposal 0020): off by default,
and it changes no statement while the setting is off. It may change or be withdrawn in a minor release. The
module docstring of `rebac.display` is its specification.

This is a proposal of its own rather than a change to 0020 because it adds a public queryset method, and because
its security rests on a condition of its own (below) that deserves review apart from what the owner accepted in
0020.

## Problem

Proposal 0020 delivers display rows for related rows that `rebac_select_related` joins: a list of messages shows
each author's name without a permission statement against the authors. An application that loads every to-one
relation by prefetch, as a GraphQL layer does (one `Prefetch(field, queryset=<actor-scoped queryset of the
target>)` per relation), never joins, so it never gets them: every label on a page (the name of a thread, of a
party, of a milestone) costs one scoped permission statement against the target type: 9 to 66 KB each in one such
application, and 102 to 171 KB for a member with many grants on the policy benchmark (below).

## The request

```python
Prefetch("author", queryset=Account.objects.with_actor(actor).for_display())
```

`for_display()` is a queryset method of `RebacQuerySet` and `RebacManager`, named after `for_write()`: a
queryset whose rows are for display. It records a request and nothing else; clones carry it. With
`REBAC_DISPLAY_FIELDS` off, or on a model that declares no display fields (or only gated ones), the queryset
reads and returns exactly what it would without the call, statement for statement. `"fallback"` and `True` mean
the same for it: the setting turns the mechanism on and the request decides.

## Scope

A label is the row a **forward foreign key (or one-to-one) names by its primary key**. That is the whole surface:
a reverse one-to-one, a foreign key with `to_field`, a to-many or many-to-many relation and a generic foreign key
are read by their scope.

## The condition

When a queryset that asks for display rows is evaluated as model instances (`RebacQuerySet._fetch_all`:
iteration, `list()`, `async for`, a prefetch's own evaluation), `rebac.display.deliverable` decides. Every one of
these must hold:

1. **The setting and the model.** The setting is on; the model declares display fields that its policy does not
   gate; its identity is its primary key. A display field is a plain column of the model's own: a foreign key or a
   one-to-one is refused where the declaration is read, for joined and prefetched display rows alike, and so is
   any declaration of a model whose primary key, or a multi-table parent's, is a relation other than a parent
   link, since a display row shows its key. System check `rebac.E026` reports such a declaration at startup,
   whatever `REBAC_DISPLAY_FIELDS` is.
2. **The shape of the queryset.** It reads model instances of its own table (Django's own iterable) and carries
   nothing of the caller's that changes a condition, a join, a column or the order of the library's read: no
   projection, annotation or alias, `extra`, `select_related`, join, `distinct`, ordering of its own (`order_by`;
   the model's `Meta.ordering` is not the caller's, and a read by key needs no order), `select_for_update`, slice
   or set combination. It may carry its actor, `with_action()`, `on_field_deny()` (the deny mode the hidden
   fields are marked with), `only()` and `defer()` (the library reads its own columns), `reverse()`,
   `fetch_mode()`, `prefetch_related()` (its lookups are read below the display rows, by their scope) and
   `using()` of the parents' database. Django's prefetch of a forward to-one clears both kinds
   of ordering before the queryset is evaluated; a binding evaluated as it stands keeps them. Its WHERE clause is
   exactly one condition, not negated: the binding Django 6.1 writes for the prefetch of a forward to-one,
   `TupleIn(ColPairs(target table, [pk], [pk], relation), [(value,), …])`, with the target's primary key as both
   its column and its source and every value a literal. `ColPairs` names the relation followed.
3. **The relation.** It is a foreign key or one-to-one, not a multi-table parent link, targeting the primary key,
   and its target model is the queryset's model itself, not a proxy or a subclass of it: the display declaration
   and the field gates are the target model's.
4. **The references.** An evaluation for the same actor is running, opened in this thread and this asyncio task
   (`display.referring`: the evaluation of a queryset scoped for that actor, and every level read inside it for
   that actor). Every key the binding asks for, compared with its type, is a value the database returned for that
   relation on a row admitted to it:
   - a row of the relation's model, or of a proxy or subclass of it (the parent side may be either), read by a
     queryset scoped for this actor and **admitted to be read** (`display.admits_reading`): under that type's read
     action (the one a plain read of it is scoped by), or under an action `A` such that the policy proves that
     whoever is granted `A` on a row of that type is granted its read action (`rebac.structural.implies`,
     [proposal 0022](./0022-structural-delivery-under-an-implied-action.md)). The proof reads the policy the backend
     evaluates; it is structural's own, kept per policy and proven afresh for a new one; while the policy carries an
     override of any kind nothing is proven and only the read action counts, nor is anything proven about a type
     that reads a name two ways. It is decided where the queryset's scope is built, and carried by its query with
     its decision to the rows it admits: what a row was admitted to is
     what the policy that admitted it says, whatever the policy is when a delivery is decided, as for a row
     admitted under the read action itself. An inbox that lists messages under `read_inbox`, which implies `read`,
     shows the labels its messages refer to; an action that grants what the read action does not (`peek`, where a
     peeker may not read) lends nothing;
   - built from the database by Django's own iterable, with no `extra(select=…)` and no set combination (whose rows
     may come from another table);
   - recorded when it was admitted from the values the database returned for it, which `RebacMixin.from_db` keeps
     (`_rebac_loaded_values`), never from its attributes: nothing set on the row after it was built (a related
     manager's known objects, a prefetch's `to_attr`, `__init__` or a `post_init` receiver, any assignment)
     changes what it refers to, and a deferred key, not returned, refers to nothing;
   - on the queryset's database, where the row's type declares no read gate on the relation's field (`read__<field>`
     disables the delivery whatever the actor holds of it).

   A display row is never admitted, so nothing is delivered through it in turn.

When they hold, **the library reads the rows itself** (`rebac.display.read`): one statement built from a bare
`QuerySet` of the model, which no manager of the application touches (a base manager's annotations, joins, extra
columns, filters or queryset class take no part), on the database the parents were admitted from: `pk IN (<the
values the admitted rows hold>)`, no other condition, no ordering, selecting the primary keys (a multi-table model's
parent keys as well) and the display columns and nothing else. Each row is then built by `model.from_db` with every
column given, the hidden ones as `None`, so nothing is deferred while the model's own code runs. The queryset's own
conditions are never executed, so the keys asked for only choose among values admitted rows hold; they are never
what the database is asked for. The check reads no row and loads no field: it consults the values recorded at
admission and the policy's field gates, which the evaluation has already read.

**Who sees more.** With the setting on, the one thing an actor can be shown that it could not be shown before is the
display fields of a row that a foreign key names, where the actor was just admitted to read a row for which the
database returned that key (under the holding row's read action, or under an action that the policy that admitted
it proves grants that read action), and the holding row's type declares no read gate on that foreign key; whether or
not the actor may read the named row.

## What falls back, and to what

Anything else is read as the queryset would be read without the request: **by its permission statement, whole
rows of those the actor may read**. That covers a queryset iterated on its own or outside an evaluation, filtered,
excluded, ordered, annotated, projected, sliced, joined, combined, `in_bulk`, `iterator()`, `count()`,
`exists()`, a reverse, to-many, many-to-many, generic or `to_field` relation, a proxy or subclass of the target, a
target a router sends to another database than its parents', a prefetch under another actor than the parents', in
a thread or a task started during the evaluation (below, Known limits, for code a task hands back to the
evaluation's thread), parents read by `sudo()` or `system_context()`, under an action not proven to imply read,
with `extra(select=…)`, as a union, through a many-to-many prefetch, with the foreign key deferred or gated, a
queryset whose query an earlier read of it scoped (below), and a binding with any key no admitted row holds.

Fallback rows are whole, not display rows: they are rows the actor may read, so delivering them whole shows
nothing the actor could not read by dropping the request, and the fallback is today's code path exactly.
`display.is_display_row(row)` says which a row is. This differs from a joined row under `True`, which is a
display row even where the actor holds it.

Whether a queryset delivers display rows is decided by an evaluation that scopes its query
(`RebacQuerySet._fetch_all`), with its scope: the evaluation's query carries the keys to read and a scope that
matches no row, and the queryset keeps nothing of it. A clone, the same queryset under another actor, bypass or
database, and an evaluation after one that raised are decided again; its query embedded in another statement, a
count and its own `iterator()` are decided where they run and never delivered; a decision that is not a delivery
reads by the permission statement. The 0.38.0 rule holds as it stands: a queryset whose query already carries a
scope for the same decision keeps that query. After its own `count()`, `exists()`, `aggregate()`, `iterator()` or
`aiterator()`, and for an eager `scoped()` queryset and its clones, an evaluation under the same actor, bypass and
database reuses that scope at the cost of one comparison and reads by its permission statement, decided neither
for display nor for structural delivery. Pickling it strips its actor, as ever.

## What a delivered row is

Exactly a joined display row (`display.deliver`), relations refused as display fields in both:

- `display.is_display_row(row)` is true; the row carries the actor.
- Its primary key and display fields are read; every other field reads as `None` and is named by
  `redacted_fields(row)` (in `"omit"` mode by `_rebac_omitted_fields` too). The read selects no other column, and
  `_rebac_loaded_values` holds the key and the display fields alone.
- The model's own code that runs while the row is built (`__init__`, a `post_init` receiver) sees every hidden field
  as `None`, and no statement loads one: nothing on the row is deferred.
- A hidden foreign key reads as absent: following it gives nothing, or raises `RelatedObjectDoesNotExist` for a
  non-null key, and a nested prefetch through it reads nothing. No display field is a relation, and no model
  whose primary key is a relation other than a parent link has display rows, so no field a display row shows
  leads to another row: a multi-table row's parent link gives its own parent part, which Django builds from the
  display row's values without a statement.
- Its other relations, followed lazily, load as they do from any row of this library; a display row adds nothing
  to that and takes nothing from it. A reverse one-to-one is loaded by Django's descriptor through the base
  manager, whole and with no permission statement (`rebac.W003` lists such relations, from the model that declares
  them); a reverse foreign key's or a many-to-many's related manager reads by its scope. The row is not admitted,
  so neither structural prefetch nor display delivery hangs off it.
- `refresh_from_db()` reads the fields the row shows and no other, as `refresh_from_db(fields=…)` with them and
  with its prefetched relations, which it forgets; the hidden fields stay `None`. Naming a hidden field in
  `fields` is refused. Model code that runs while Django builds the refreshed values (`__init__`, `post_init`)
  finds the hidden fields deferred; code that reads one loads it with a statement of its own.
- `save()` needs the actor's own `write` permission and writes no hidden column. A save that names no fields
  (`save()`, `asave()`, `save_base()`, a raw one included) leaves out a hidden field that holds its placeholder
  (`None`, or what the field's descriptor makes of it when read, such as a `FieldFile` with no name), and refuses,
  before any statement, one that holds anything else, a value given to it, which it would not write; a `None`
  assigned there cannot be told from the placeholder and is left out. A save that would insert a row from it
  (`force_insert=True`, the row marked as being added, a primary key other than the one loaded) is refused before
  any statement. With no shown field changed, nothing is written and no `pre_save` or `post_save` is sent. So a
  related manager's `add(bulk=False)` of a display row is refused, and its `remove()` raises Django's own
  `DoesNotExist`, since the row's foreign key reads `None`.
  A write that names a hidden field is refused, under any actor or bypass, as for a row whose gated fields were
  redacted (`rebac.field_visibility.refuse_hidden_fields`): `save()` and `asave()` with `update_fields`,
  `bulk_update()` and `abulk_update()` through any manager (a related manager's and the base manager's included),
  the bulk ones before any statement. `bulk_create()` and `abulk_create()` of a list that holds a display row are
  refused before any statement, in every form and through any manager: a row with hidden fields is not copied by
  any insert.
- A display field that carries a read gate is not displayed and not selected: the gate decides it, never the
  declaration.

An actor who may read the referred row whole gets the display row too, and loses its hidden fields in that read.
`compare_mechanisms` states the change. It compares results by what they loaded: a model instance in a result is
its model and the values of the concrete fields it holds, so a row read whole and the same row as a display row
differ, though Django finds them equal. For a mapping result, as before, an item is a key with its value, so a row
that changed is one item lost and one gained; the new `changed` field counts the keys whose value differs.

## Measured

The policy benchmark (`docs/ARCHITECTURE.md § The policy benchmark`) at its small size (7,900 model rows), the
application policy of proposal 0020, registry layout, PostgreSQL 17, JIT off. Display fields are declared for three
label types: threads, parties and files. `labels` reads one message with its thread, its sender (a handle, read by
its scope) and the sender's party, and its parts (by their scope) and their files: three label prefetches.
`page_labels` reads a thread's newest messages (four at this size) with the thread and each sender's party.
Statements / SQL / wall time, median of 5; the time is the range of two runs.

| Read | Actor | Today | Display rows | Every other setting | … and display rows |
|---|---|---|---|---|---|
| `labels` | heavy member | 11 / 652 KB / 596–691 ms | 10 / 265 KB / 248–255 ms | 11 / 428 KB / 358–423 ms | 10 / 116 KB / 111–123 ms |
| `labels` | light member | 7 / 296 KB / 217–255 ms | 6 / 143 KB / 107–135 ms | 7 / 180 KB / 176–188 ms | 6 / 59 KB / 59–65 ms |
| `labels` | administrator | 8 / 9.8 KB / 19 ms | 8 / 7.2 KB / 16–19 ms | 8 / 9.2 KB / 14–22 ms | 8 / 6.6 KB / 12–17 ms |
| `page_labels` | heavy member | 8 / 354 KB / 247–318 ms | 8 / 145 KB / 128–145 ms | 8 / 280 KB / 266–281 ms | 8 / 116 KB / 126–138 ms |
| `page_labels` | light member | 5 / 189 KB / 124–161 ms | 5 / 75 KB / 58–69 ms | 5 / 148 KB / 109–137 ms | 5 / 58 KB / 46–59 ms |
| `page_labels` | administrator | 6 / 8.0 KB / 10–13 ms | 6 / 6.3 KB / 10–11 ms | 6 / 8.0 KB / 11–12 ms | 6 / 6.3 KB / 10–14 ms |

- Each label's scoped statement (102 KB for a thread, 107 KB for a party, 171 KB for a file, for the heavy member)
  becomes the library's read by primary key, 0.1 KB. The file's permission also cost a 7.6 KB kept-decision
  statement before its scope; with display rows no file permission is compiled, hence one statement fewer.
- What remains is the root and the relations that are not labels (the sender's handle, the parts), each still
  read by its scope.
- The administrator's statements fold to a constant arm already: little to save, and the times at that size are
  within the noise of a loaded machine.
- The read that does not ask for display rows (`selected`) issues the same statements, byte for byte, with the
  setting on.
- These reads return display rows where today returns whole rows or nothing, so the benchmark marks them
  `DIFFERS`: that is the feature, not a fault.

## Verification

Parents admitted under an implied action (`tests/test_display_prefetch.py`): delivery under an action that implies
read and none under one that does not; the proof kept per policy and proven afresh; nothing but the read action
while an override is stored; a policy replaced between admission and delivery, both ways, and while the parents are
built; the gate and a key no admitted row holds; a target's own ordering, in Django's prefetch and in a binding
evaluated as it stands. The scope rule with display rows on: a display read reused under another actor, embedded,
iterated in another context, and evaluated again after it raised.

`tests/test_display_prefetch.py`: the request changes no statement where it does not apply (statement text compared,
on PostgreSQL too); delivery with no permission statement, counted and classified, under both settings, with
`to_attr`, with the root's actor taken by `rebac_prefetch_related`, inside `async for`; every fallback, the reverse
one-to-one (to the key and to a gated `to_field` column), `to_field`, a proxy, another action, a root with
`extra(select=…)`, a union, a deferred key in the root and elsewhere in the evaluation (no extra statement), a key
assigned after admission; forged bindings inside a running evaluation (extra keys, another column, another relation,
another source, negated, combined, a key equal only across types, another actor, an actor's evaluation nested in
another's, another database, projection, ordering, slicing, `in_bulk`, `iterator`, `count`); a key forced into
Django's own prefetch; reuse of one queryset across two prefetches; pickling; nested prefetches; gates on the
display field, on the parent's relation and on a proxy parent's own type; a target whose identity is not its key;
the exact columns of the library's read, unchanged by a base manager that annotates, selects extra, joins, filters
or builds rows its own way; a relation refused as a display field (prefetched and joined); a multi-table target, and
a multi-table parent referring by its parent's key; model code that runs while a display row is built (`__init__`,
`post_init`), with the statement count; known related objects and a `post_init` receiver rewriting a parent's key;
another actor reading inside the evaluation; rows of another iterable or not built by the database; rows read on
another database; a thread and a task started inside the evaluation, a thread opening its own, and a context run
after the evaluation ended; a many-to-many prefetch; what a delivered row does; random application-shaped policies
in both storage layouts; `compare_mechanisms`. Then: code an asgiref task hands back to the evaluation's thread; an
iterator's chunks inside and outside a running evaluation; a queryset an earlier `count()`, `exists()`,
`iterator()`, `aggregate()` or `scoped()` scoped; what a display queryset may carry and still be delivered;
`_fetch_all` offering display rows to no record; an action over a recursive read; every write that names a hidden
field, through each path, under an actor and a bypass, with no statement sent, the names given as a generator too;
every save that names no fields (`save()`, `asave()`, `save_base()`, raw), with each hidden field holding `None` and
with one given a value, with nothing changed (no statement, no signal), and saved as a new row, `bulk_create()` of a
display row in every form, a related manager's `add()` and `remove()`, a multi-table row's save and `loaddata`
(`tests/test_display_prefetch.py`, and `tests/test_field_read_gates.py`, file, image, foreign-key and generic fields
read through their descriptors included, and `tests/test_check_save.py` for redacted rows); a display row's
relations followed lazily; the statement a refresh sends and what model code sees during it, and a refresh leaving
deferred fields deferred (`tests/test_security_related_writes.py`); a display row's equality; a model whose key is a
one-to-one, and a multi-table child of it, and `rebac.E026` under every setting (`tests/test_display_fields.py`);
`compare_mechanisms` on rows. Defects planted one at a time each fail them (the table is in the change's report).

## Known limits

- **Where it applies.** The evaluation of a queryset (`list()`, iteration, `async for`) runs its prefetches inside
  a running evaluation. The chunks of `iterator()` and `aiterator()` do not open one (as for structural prefetch)
  and admit none of their rows: their to-one prefetches are delivered only inside a running evaluation for the
  same actor, for the keys its admitted rows hold, and are read by their scope otherwise.
- **One evaluation, its thread and its task.** The record of an evaluation serves, for its actor, code that runs
  with its context in the thread and the asyncio task that opened it (none, outside a task), until the evaluation
  ends. A thread started during the evaluation starts with an empty context on the default build, and with a copy
  of its starter's on a free-threaded build or when it is given one (`copy_context().run`, `Thread(context=…)`); a
  task (`create_task`, `asyncio.run`) runs with a copy; a context copied during the evaluation may be run after
  it. In none of them does the record refer to anything, and a `for_display()` prefetch there is read by its
  scope. Code such a task hands back to the evaluation's thread is served: asgiref's `async_to_sync`, called during
  the evaluation, runs its coroutine in a thread of its own, and a thread-sensitive `sync_to_async` call in it (the
  default) runs back in the evaluation's thread, outside any task, with the evaluation's context. Thread and task
  identity cannot tell that code from the evaluation's own: it is served, for the same actor, until the evaluation
  ends.
- **A display row is a display row for every reader.** `for_display()` returns display rows even for rows the
  reader may read whole: switching a prefetch to it loses the other fields of those rows. A display row equals,
  and hashes as, the same row read whole (Django compares model and key), so a set, a deduplication or a dict keyed
  by instance can hold one in place of the other.
- **`rebac.display.referring()` and `rebac.display.admit()` are trusted.** They record rows as admitted with no
  check that a scope read them and no bypass audit: code that holds rows and calls them makes any foreign-key target
  of those rows displayable to the actor it names, while the block runs. They are the seam of the library's
  querysets and of trusted framework code, not a public door.
- **The decision is made whether it is used.** With the setting on, every actor-scoped queryset whose scope is
  built decides whether its action admits its rows to be read: a comparison for the read action; for any other,
  the lookup of the policy the backend evaluates and of a kept proof, about 12 µs on the test suite's in-process
  policy, and no statement added to the read (in-process or stored policy); the first read of a policy and action
  runs the proof.
- **No action is proven to imply a recursive read.** `rebac.structural.implies` never opens a recursive name on the
  side it proves, so under any action but the read action itself (even `permission browse = read`), rows of a
  type whose read action is recursive refer to nothing.
- **A multi-table display row's parent link** gives Django's instance of the parent model, built from the display
  row's values: its hidden fields are `None` there and nothing marks them, so saving that instance writes them. A
  field-redacted multi-table row does the same.
- **Many-to-many prefetches never refer.** Django builds that prefetch with `extra(select=…)`, so its rows are not
  admitted, and a label prefetched through them is read by its scope.
- **A target a router sends elsewhere** than its parents' database is read by its scope.
- **What is trusted** is what produces the values the database returned: the database, a custom field's
  `from_db_value`, and a model's own `from_db` override that changes the values before the library's records them.
- **Consistency.** The display fields are read in a later statement than the parents, each seeing the state
  committed when it runs; the reference is the one the parent held when it was read. Inside one snapshot (proposal
  0020 § Known limits) it is exact.
- **Only Django 6.1's binding is recognised.** Another Django version's prefetch is read by its scope until it is.
