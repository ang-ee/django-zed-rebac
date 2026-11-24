# The policy benchmark

A real project's policy, run over generated data of any size. It answers what a permission costs once there is
data behind it, which a project's own test fixtures cannot: they are a few rows, and a policy's cost in planning
and in execution only shows against millions.

## How it works

1. **Export.** Inside the project, `rebac.skeleton.export("out")` writes `policy.zed` (the effective policy) and
   `skeleton.json`: for every model the policy touches, its table and key, its relations to the other such models
   and the columns a backing's filters read. No row is read.
2. **Stand-ins.** `tests.benchapp` rebuilds one model per skeleton entry, with the original's table and relation
   names, so the policy resolves against them exactly as against the originals.
3. **Data.** `tests.bench.data` fills every table with one `INSERT … SELECT` from the row number, and writes
   relationship rows for every stored relation: background grants for a share of the objects, and the grants of
   a fixed cast of users.
4. **Reads.** A policy's `profile.py` holds the reads an application makes (a page of threads, one message with
   everything a reader shows, a bounded check), each loading its relations as scoped prefetches.

`policies/angee/` is the export of [ang-ee/angee-django](https://github.com/ang-ee/angee-django) at the commit
named in its `skeleton.json`: 147 definitions over 142 models. A message's read permission reaches 125 relations.

## Running it

```sh
make bench-smoke                         # tiny, in-memory SQLite, every scenario; fails if a result differs

export DJANGO_SETTINGS_MODULE=tests.settings_bench
export REBAC_TEST_POSTGRES_URL=postgresql://user:password@host:5432/bench    # an empty database
python -m tests.bench.run build --size medium
python -m tests.bench.run read --size medium --actor heavy,light,admin --mechanism today,all --explain
python -m tests.bench.run census --size medium
```

| Size | Unit | Model rows | Relationship rows | Build, PostgreSQL 17 |
|---|---|---|---|---|
| `tiny` | 4 | 1,100 | 600 | under a second |
| `small` | 40 | 7,900 | 4,800 | 1 s |
| `medium` | 2,000 | 383,000 | 231,000 | 6 s |
| `large` | 80,000 | 15.3 million | 5.6 million | 3.7 minutes, 4.3 GB |

`--size` also takes a unit count. Environment: `REBAC_BENCH_STORAGE` (`registry`, the default, or
`denormalized`), `REBAC_BENCH_JIT=on` (PostgreSQL's JIT is off otherwise), `REBAC_BENCH_TIMEOUT=120s` (a read past
it is reported, not waited for), `REBAC_BENCH_POLICY` (the export to use), `REBAC_TEST_POSTGRES_BINDING=server`
(parameters bound on the server and every statement prepared, as `tests/settings_postgres.py` describes).

`read --uncommitted` runs each read inside a transaction that has written one relationship and not committed
it, which is how a test that seeds grants in its own transaction reads: the library then keeps no decision and
reads the actor's profile at every compile.

`read` prints one line per scenario, actor and mechanism: rows read, statements, their SQL size, the wall time
(median of `--repeats`), and with `--explain` on PostgreSQL the planning time, the execution time and the
planner's memory, summed over the statements. A mechanism is a named set of settings (`today`, `structural`,
`rows`, `named`, `walk`, `trusted`, `noprofile`, `all`, and the bounded reads of proposal 0025: `bounded_keys`,
`bounded`, `all_bounded`); a line ends in `DIFFERS` if it read something else than the first mechanism did, and
under a bounded mechanism it says how many list reads were decided by their candidates. `--statements FILE`
writes every statement a run sent, with its parameters, to compare two checkouts.
`python -m tests.bench.candidates` explains where a read's candidates are decided and what each statement costs
(`plans`, `statements`), and reads a page for readers of a known small share of a model (`sparse-setup` on a
scratch database named `sparse_*`, then `sparse`). `plain` is `REBAC_ENABLED = False`: Django's own queries, with every row
readable, as the floor the others can be compared with.

## The cast

| User | Holds |
|---|---|
| `admin` | Membership of every role the policy names as a constant |
| `heavy` | Direct grants on up to 25,000 rows of each main type |
| `teams` | Membership of 200 groups that hold grants on the main types |
| `light` | One group and five direct grants per main type |
| `author` | The grant on up to 100,000 rows of each one-grant-per-row relation, nothing else |
| `sparse` | Three grants, at the end of each main type's id range |
| `outsider` | Nothing |

Every other user is background: in two groups, with grants on a share of the objects.

## What it does not model

- The layer above the ORM: how a GraphQL document becomes queries, and mutations.
- Real distributions. Keys are a hash of the row number, so the data has no skew beyond what the cast and the
  profile put there. A project can calibrate a profile from counts of its own database (rows per table, grants
  per user, group sizes) without exporting a row.
- Writes: the relationship table is filled directly, once.

## Adding a policy

Export it, put the two files under `policies/<name>/` with an empty `__init__.py`, and write `profile.py` beside
them: `ROWS` (rows per unit for the models that matter), the names the generator needs (`MAIN_TYPES`,
`ONE_PER_ROW`, `TEXTS`, `COMMONLY_TRUE`, `INERT`, `FOREIGN_KEYS` for locality) and `SCENARIOS`. Then
`REBAC_BENCH_POLICY=<name>`.
