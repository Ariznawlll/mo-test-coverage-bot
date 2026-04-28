# big-data-test

Reference for generating / extending **big-data regression** cases that live
in `matrixorigin/mo-nightly-regression` on branch `big_data` (note the
underscore — not `big-data`). Load for these cases is driven from Tencent
COS; the table set is fixed and already covers most scenarios, so **prefer
adding queries against existing tables over introducing new datasets**.

## When to add big-data coverage

Changes in these paths typically need at least one query-level case here:

- `pkg/sql/plan/`             — filter pushdown, domain normalization, join reorder, predicate simplification
- `pkg/sql/colexec/`          — vectorized operators, aggregation, sort, hashbuild/join
- `pkg/sql/compile/`          — physical plan shape, pipeline, dispatch
- `pkg/vm/engine/disttae/`    — read path, tombstones, block scan, prefetch
- `pkg/fileservice/`          — S3 / object-storage read + write behavior on large objects
- `pkg/txn/` touching large-table DML paths

Pure unit / lexer / docs / tooling changes do **not** need coverage here.

## Target repo, branch, PR base

- Repo: `matrixorigin/mo-nightly-regression` (or a fork)
- Branch and PR base: **`big_data`** (single word, underscore)
- The upstream nightly workflow for this suite lives at
  `.github/workflows/big-data-test.yml` on the same branch.

## Directory layout

```
tools/mo-regression-test/
├── cases/big_data_test/
│   ├── 01_CREATE_TABLE/       schema DDL (one 100M and one 1B per variant)
│   ├── 02_LOAD_DATA/          `load data url s3option { ... }` from COS
│   ├── 03_INSERT_SELECT/      insert ... select between sibling tables
│   ├── 04_QUERIES/            ← new query coverage goes here
│   ├── 05_ALTER_TABLE/
│   ├── 06_CLONE/
│   └── 07_WINDOW/
└── golden/big_data_test/<same layout>/<name>.result
```

Add a query file to `04_QUERIES/` with the next free numeric prefix
(current max is 23; use 24+). **Do not modify files under `01_` or `02_`** —
they define the schema and load every downstream `.result` depends on.

## Case file format

A single `.sql` file can hold many cases. Each is delimited by a `-- @name`
tag that the runner uses as the case identifier:

```sql
-- @<snake_case_name>
<single SQL statement terminated by `;`>
```

Rules:
- Tag names: `^[a-z][a-z0-9_]*$`, ≤ 50 chars, unique within the suite.
- Keep each case to one statement. Multi-statement setup/teardown belongs in
  `01_` or `02_`.
- Use schema-qualified table names (`big_data_test.table_basic_for_load_100M`).
- Do **not** rely on row order unless you add `order by`.

## Available tables (already loaded, fixed schema)

All tables live in schema `big_data_test` and share the same 25-column
column layout (`col1..col25`), differing only in keys / sort keys / size:

| table                                        | rows | keys / cluster                         | typical use                |
|----------------------------------------------|------|----------------------------------------|----------------------------|
| `table_basic_for_load_100M`                  | 100M | none                                   | **default for new cases**  |
| `table_basic_for_load_1B`                    | 1B   | none                                   | same but stress-scale      |
| `table_with_pk_for_load_100M` / `_1B`        | 100M / 1B | `id bigint primary key` (extra col) | point lookup, pk scan      |
| `table_with_pk_index_for_load_100M` / `_1B`  | 100M / 1B | `id` pk + `key(col3)` + `unique key(col4)` | index selection       |
| `table_with_com_pk_index_for_load_100M` / `_1B` | 100M / 1B | composite pk + secondary index | composite pk scenarios     |
| `table_with_sortkey_for_load_100M` / `_1B`   | 100M / 1B | `cluster by (col12)`                   | zonemap / sortkey pruning  |

Variants named `_for_insert_*`, `_for_write_*`, `_for_alter_*` are
reserved for `03_INSERT_SELECT/`, DML, and ALTER cases; do not read them
in `04_QUERIES/` — they may be empty or in flux.

For most `04_QUERIES/` work, **default to
`table_basic_for_load_100M`** unless the change specifically targets
pk/index/sortkey behavior.

## Column layout on `table_basic_for_load_100M`

Shared by all `_for_load_*` tables. Distribution notes are from
a 5k-row head sample of the 100M dataset (stable enough for literal
selection; do not rely on exact counts).

| col     | type                 | distribution on 100M                     | good for                                    |
|---------|----------------------|------------------------------------------|---------------------------------------------|
| col1    | `tinyint`            | 256 values uniform, ~390K rows each      | `IN` / `NOT IN` / `=` / `<>` literal tests  |
| col2    | `smallint`           | ~65K values uniform, range ≈ [-32753, 32761] | `BETWEEN`, range pred                   |
| col3    | `int`                | full-range random int32                  | aggregates, **not** IN-literal tests        |
| col4    | `bigint`             | full-range random int64                  | aggregates                                  |
| col5    | `tinyint unsigned`   | 256 values uniform                       | `IN` / `NOT IN`                             |
| col6    | `smallint unsigned`  | ~65K values uniform                      | range pred                                  |
| col7    | `int unsigned`       | full-range random uint32                 | aggregates                                  |
| col8    | `bigint unsigned`    | full-range random uint64                 | aggregates                                  |
| col9    | `float`              | continuous ±1e8                          | float aggregates, **avoid equality**        |
| col10   | `double`             | continuous ±1e8                          | double aggregates                           |
| col11   | `varchar(255)`       | mixed length 1–255, pseudo-random suffix | `LIKE`, length, substring                   |
| col12   | `Date`               | range 1000-01-01 .. 9999-12-31           | date range, year extract                    |
| col13   | `DateTime`           | full range                               | datetime arithmetic                         |
| col14   | `timestamp`          | full range                               | timestamp ops                               |
| col15   | `bool`               | ~50 / 50                                 | `=` / `<>`                                  |
| col16   | `decimal(16,6)`      | ±~1e8 with 6 decimals                    | decimal aggregates                          |
| col17   | `text`               | mixed length                             | `LIKE`, length                              |
| col18   | `json`               | object with one random key               | json path extract                           |
| col19   | `blob`               | short random bytes                       | bytes length                                |
| col20   | `binary(255)`        | fixed-width random bytes                 | binary comparison                           |
| col21   | `varbinary(255)`     | **21 distinct 3-byte values**, ~4.7M rows each | low-cardinality `IN`, group-by       |
| col22   | `vecf32(3)`          | 3-dim float32 vector                     | vector ops (`l2_distance`, etc.)            |
| col23   | `vecf32(3)`          | 3-dim float32 vector                     | same                                        |
| col24   | `vecf64(3)`          | 3-dim float64 vector                     | vector ops                                  |
| col25   | `vecf64(3)`          | 3-dim float64 vector                     | same                                        |

## Verified literals (for deterministic IN/NOT IN tests)

Values confirmed present in `table_basic_for_load_100M` via sampling:

- **col1** (tinyint): `13, -29, -119, 125, -53, 77, 27, -113, -78, -26` — each matches ~390K rows. `999` is out of range and matches 0.
- **col5** (tinyint unsigned): `254, 255, 222, 202, 9, 87, 8, 29, 144, 133`.
- **col21** (varbinary 3-byte): `'rst', 'igk', 'gkl', 'opq', 'def', 'nop', 'hig', 'pqr', 'fgh', 'tuv'` — each ~4.7M rows.

Prefer picking literals from these lists when the test needs to hit real
rows. Use a value outside the set (e.g. `col1 = 999`) to construct the
"must match zero rows" branch.

## Golden files — don't generate them here

`golden/big_data_test/<dir>/<name>.result` files are produced by **running
the case against a real 100M-row MO (or MySQL) instance** and capturing
the output. Autogenerated `.result` payloads are almost always wrong,
because they must match the real column distribution byte-for-byte.

Workflow for reviewers:

```bash
cd tools/mo-regression-test
python3 run.py -c cases/big_data_test/04_QUERIES/<file>.sql
# or, for pure SQL-compatible cases:
bash gen_golden_mysql.sh
cp results/big_data_test/04_QUERIES/<name>.result \
   golden/big_data_test/04_QUERIES/<name>.result
```

Commit the `.result` alongside the `.sql` in the same PR.

## JSON output schema (for generators)

When a generator (LLM or script) emits a new case, the payload MUST be:

```json
{
  "test_name": "snake_case_name",
  "summary": "one-line description of what's exercised",
  "module": "04_QUERIES",
  "sql_content": "-- @case1\nselect ...;\n-- @case2\nselect ...;\n"
}
```

- `test_name`: `^[a-z][a-z0-9_]{2,59}$`. Becomes the filename: `NN_<test_name>_100M.sql` where `NN` is the next free prefix in the target directory.
- `module`: one of `04_QUERIES` / `05_ALTER_TABLE` / `06_CLONE` / `07_WINDOW`. New query coverage goes to `04_QUERIES`.
- `sql_content`: raw file body, `-- @name` tagged, schema-qualified table names, no `.result` output.

If the change doesn't need big-data coverage, emit `{"skip": true, "reason": "..."}` instead.

## Things not to do

- Don't create `.result` / golden files in generator output.
- Don't introduce new S3 datasets or `load data` statements (changes `02_LOAD_DATA/` affect every downstream case's golden).
- Don't change `01_CREATE_TABLE/` column types.
- Don't cross-reference `*_for_insert_*`, `*_for_write_*`, `*_for_alter_*` tables from `04_QUERIES/` — they're owned by their respective directories.
- Don't use float equality on col9/col10 or exact decimal equality on col16 — prefer aggregates or range predicates.
- Don't fabricate `.result` "expected" payloads; leave them to the reviewer.
