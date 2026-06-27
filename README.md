# MariaDB InnoDB Migrator

![version](https://img.shields.io/badge/version-v0.9-blue)
![license](https://img.shields.io/badge/license-MIT-green)

A single-file Python tool for migrating a live MySQL/MariaDB database into a new
database while forcing every table to `ENGINE=InnoDB`. It is designed for
**low-downtime migrations of production databases**: the bulk of the data is
copied while the application keeps running, writes are captured via triggers, and
only the final cut-over requires a short stop.

> **Version:** v0.9 — check yours with
> `python mysql_live_migrate_innodb.py --version`.

## Features

- **Live, resumable bulk copy** — tables are copied in primary-key-ordered chunks
  using keyset pagination and `INSERT ... ON DUPLICATE KEY UPDATE`, so a run can
  be interrupted and resumed at any time without duplicating data.
- **Change capture via triggers** — `AFTER INSERT/UPDATE/DELETE` triggers on the
  source record every write into a change log while the initial copy runs.
- **Delta replay on cut-over** — captured changes are applied to the destination
  during a short downtime window, then the migration triggers are removed.
- **Automatic InnoDB conversion** — `SHOW CREATE TABLE` output is rewritten to
  `ENGINE=InnoDB`, stripping MyISAM/Archive/Merge-era table options that would
  otherwise fail (`ROW_FORMAT`, `PACK_KEYS`, `CHECKSUM`, `DELAY_KEY_WRITE`,
  `KEY_BLOCK_SIZE`, `DATA/INDEX DIRECTORY`, `UNION`, `INSERT_METHOD`, `RAID_*`,
  `TABLESPACE`).
- **Throttling** — a configurable sleep between chunks keeps load off a busy
  production server.
- **Row-count verification** — a `check` mode compares `COUNT(*)` per table
  between source and destination.

## How it works

The tool maintains two bookkeeping tables in the **destination** database:

| Table                 | Purpose                                                                 |
| --------------------- | ----------------------------------------------------------------------- |
| `__migration_state`   | Per-table resume cursor (last copied primary key, row count, status).   |
| `__migration_changes` | Change log written by source triggers (`I`/`U`/`D` + primary key JSON). |

A migration runs in one of four modes, selected in the config file:

1. **`copy`** — creates the destination database, recreates each base table as
   InnoDB, installs the change-capture triggers, then chunk-copies all rows.
   Resumable and idempotent. Views are (re)created at the end. Run this **while
   the application is still live**.
2. **`final`** — replays the captured change log into the destination, recreates
   views, and (optionally) drops the migration triggers. Run this **after the
   application is stopped**.
3. **`check`** — read-only row-count comparison between source and destination.
4. **`cleanup`** — drops **every** migration trigger (`mig_*`) from the source
   schema, including orphaned ones whose table was dropped or renamed. A safety
   net for abandoned migrations or for tearing down the destination cleanly.

## Requirements

- Python 3.7+
- [`PyMySQL`](https://pypi.org/project/PyMySQL/)
- A MySQL/MariaDB user with privileges to read the source, create the destination
  database and tables, and create triggers (e.g. `SELECT`, `CREATE`, `INSERT`,
  `UPDATE`, `DELETE`, `TRIGGER`, `CREATE VIEW`).

```bash
pip install pymysql
```

## Installation

```bash
git clone https://github.com/endremadarasz/mariadb-innodb-migrator.git
cd mariadb-innodb-migrator
pip install pymysql
```

## Configuration

Behavior is driven entirely by a config file (a small Python module). It must
define two dictionaries: `MYSQL` and `MIGRATION`.

The repository ships ready-to-use example files for each mode — copy the one you
need and fill in your credentials:

| Example file                | Mode      | Purpose                                 |
| --------------------------- | --------- | --------------------------------------- |
| `config.example.py`         | `copy`    | Initial live copy.                      |
| `config.check.example.py`   | `check`   | Row-count verification.                 |
| `config.final.example.py`   | `final`   | Cut-over: apply deltas + drop triggers. |
| `config.cleanup.example.py` | `cleanup` | Remove all migration triggers.          |

Copy the template(s) you need to the matching `config*.py` name (dropping the
`.example` part) and fill in your credentials. These real configs are git-ignored,
so your credentials never get committed:

```bash
cp config.example.py       config.py          # then fill in MYSQL credentials
cp config.check.example.py config.check.py
cp config.final.example.py config.final.py
```

The default config (used when `--config` is omitted) is `config.py`.

```python
# config.example.py

MYSQL = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "migrator",
    "password": "CHANGE_ME",
    "source_db": "old_db",
    "dest_db": "new_db",
}

MIGRATION = {
    # copy | final | check
    "mode": "copy",

    # Rows per chunk during the bulk copy.
    "chunk_size": 1000,

    # Seconds to sleep between chunks (throttle). 0.20 = 200ms.
    "sleep": 0.20,

    # Batch size when applying the change log (mode = final).
    "change_batch_size": 1000,

    # Re-read PK tables from the beginning using upserts. Usually False.
    "full_rescan": False,

    # Drop migration triggers after applying changes. Set True only on the
    # final run, after the application is stopped.
    "drop_triggers": False,
}
```

### Configuration reference

| Key                 | Required | Default | Description                                            |
| ------------------- | -------- | ------- | ------------------------------------------------------ |
| `host`              | yes      | —       | Database host.                                         |
| `port`              | yes      | —       | Database port.                                         |
| `user`              | yes      | —       | Database user.                                         |
| `password`          | yes      | —       | Database password.                                     |
| `source_db`         | yes      | —       | Source schema to migrate from.                         |
| `dest_db`           | yes      | —       | Destination schema to migrate to (created if missing). |
| `mode`              | no       | `copy`  | `copy`, `final`, `check`, or `cleanup`.                |
| `chunk_size`        | no       | `1000`  | Rows per chunk during bulk copy.                       |
| `sleep`             | no       | `0.20`  | Seconds slept between chunks.                          |
| `change_batch_size` | no       | `1000`  | Changes applied per batch in `final` mode.             |
| `full_rescan`       | no       | `False` | Re-copy PK tables from the start via upsert.           |
| `drop_triggers`     | no       | `False` | Remove migration triggers after `final`.               |

> **Note:** `source_db` and `dest_db` must be different databases on the same
> server (the tool copies across schemas within one connection).

## Usage

A typical migration with minimal downtime:

```bash
# 1. Initial long-running live copy (application stays online)
python mysql_live_migrate_innodb.py --config config.py

# 2. Verify row counts
python mysql_live_migrate_innodb.py --config config.check.py

# 3. Stop the application

# 4. Apply captured changes and drop the migration triggers
python mysql_live_migrate_innodb.py --config config.final.py

# 5. Final row-count check
python mysql_live_migrate_innodb.py --config config.check.py

# 6. Reconfigure the application to use the new database
```

If `--config` is omitted, `config.py` is used. The copy mode is fully resumable:
if it is interrupted (e.g. `Ctrl+C`), simply re-run the same command to continue.

To remove **all** migration triggers from the source — for example after an
abandoned migration, or before dropping the destination database:

```bash
python mysql_live_migrate_innodb.py --config config.cleanup.py   # mode=cleanup
```

### Recommended workflow for a production app

For a large production database (e.g. 50–100 GB), the migration is split into one
long online phase and one short cut-over phase:

1. **Run the initial copy once, while the app stays online** (`mode: copy`). On a
   50–100 GB database this typically runs for **many hours — possibly a day or
   more** — but it is deliberately gentle: small chunks plus the inter-chunk
   sleep keep CPU and disk I/O low so the live application is unaffected. The
   change-capture triggers are installed at the start of this phase, so every
   write made by the app during the copy is recorded. If it is interrupted, just
   re-run it — it resumes where it left off.
2. **Check the results** (`mode: check`) and confirm row counts line up. Run this
   as many times as you like while the app is still live.
3. **Stop the application.** This begins the only real downtime window.
4. **Run the final cut-over once** (`mode: final` with `drop_triggers: True`).
   Because the bulk of the data was already copied in step 1, this phase only
   replays the changes captured since then, so it is **fast — minimizing app
   downtime**. It also drops the migration triggers when finished. Run a final
   `check` to confirm.
5. **Reconfigure the application to use the new database**, then start it again.

Steps 1–2 happen with zero downtime; only steps 3–5 require the app to be down,
and step 4 is designed to be as quick as possible.

### Tuning for large databases

For a 50–100 GB production database, start conservative:

```python
"chunk_size": 1000,
"sleep": 0.20,
```

Then watch server load, disk I/O, and the slow-query log. Increase `chunk_size`
and lower `sleep` only while the server stays calm.

## Limitations & warnings

This tool is built for a **controlled, low-load migration**. It is not magic —
review these constraints before relying on it.

- **Primary keys are required.** Live incremental sync depends on primary keys.
  Tables without a primary key are copied once in full and **cannot** have their
  updates or deletes captured; if the destination already contains rows, such a
  table is skipped entirely to avoid duplicates. The tool prints a warning
  listing any primary-key-less tables.
- **What is copied:** tables, data, and views.
- **What is *not* copied:** users, grants, stored procedures, functions, events,
  and the source's own (non-migration) triggers. Migrate these separately.
- Test the full sequence against a staging copy before running it in production,
  and keep a backup.

### Migration trigger lifecycle

The change-capture triggers (`mig_<table>_ai/_au/_ad` on the source) have a few
behaviors worth understanding before running in production:

- **A failed or interrupted `copy` run leaves its triggers in place.** This is
  intentional — they keep capturing writes. Tables the run had not yet reached
  have no triggers yet; re-running `copy` heals this, because each table's
  triggers are dropped and recreated from scratch on every pass.
- **Re-running `copy` does not duplicate triggers, and captured changes are not
  applied twice.** Trigger names are deterministic and dropped-then-recreated
  each run, and change application is idempotent (upsert/delete by primary key).
  Replaying the same change has no additional effect.
- **Triggers are only removed when you run `final` with `drop_triggers: True`.**
  The `copy` mode never removes them. **If you never run the cut-over (or run it
  with `drop_triggers: False`), the triggers remain on the source indefinitely.**
  Since every trigger writes into `dest_db.__migration_changes`, dropping the
  destination database or that table while the triggers still exist will cause
  **every INSERT/UPDATE/DELETE on the source tables to fail.** Always complete a
  `final` run with `drop_triggers: True`, or run **`cleanup` mode** (see below),
  before tearing down the destination.
- **Some stale triggers are not auto-cleaned** by `copy`/`final`. A table that is
  dropped or renamed in the source after triggers were installed, or a table that
  loses its primary key between runs, can leave an orphaned trigger. **`cleanup`
  mode** removes every `mig_*` trigger from the source regardless of the current
  table list, which is the reliable way to guarantee a clean source schema.
- **Very long table names can collide.** Trigger names are truncated to 48
  characters, so two tables sharing the first ~44 characters of their (sanitized)
  name would map to the same trigger name and clobber each other. Keep this in
  mind if your schema has very long, similarly-prefixed table names.

## Security

The example `config.py` may contain credentials. **Do not commit real
credentials.** Keep production config files out of version control (this repo
ignores `config.py` via `.gitignore`) and grant the migration user only the
privileges it needs.

## Contributing

Issues and pull requests are welcome. Please open an issue to discuss substantial
changes before submitting a PR.

## License

Released under the [MIT License](LICENSE).
