#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import pymysql
from pymysql.cursors import DictCursor


MIGRATION_STATE_TABLE = "__migration_state"
MIGRATION_CHANGE_TABLE = "__migration_changes"


def read_version():
    version_file = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "VERSION"
    )
    try:
        with open(version_file, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


__version__ = read_version()


def q(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def load_config_file(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    spec = importlib.util.spec_from_file_location("migration_config", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load config file as a Python module: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "MYSQL"):
        raise RuntimeError("Config file must define MYSQL = {...}")

    if not hasattr(module, "MIGRATION"):
        raise RuntimeError("Config file must define MIGRATION = {...}")

    mysql = module.MYSQL
    migration = module.MIGRATION

    required_mysql_keys = [
        "host",
        "port",
        "user",
        "password",
        "source_db",
        "dest_db",
    ]

    for key in required_mysql_keys:
        if key not in mysql:
            raise RuntimeError(f"Missing MYSQL config key: {key}")

    defaults = {
        "mode": "copy",
        "chunk_size": 1000,
        "sleep": 0.20,
        "change_batch_size": 1000,
        "full_rescan": False,
        "drop_triggers": False,
    }

    merged = {}
    merged.update(mysql)
    merged.update(defaults)
    merged.update(migration)

    valid_modes = {"copy", "final", "check", "cleanup"}

    if merged["mode"] not in valid_modes:
        raise RuntimeError(
            f"Invalid MIGRATION mode: {merged['mode']}. "
            f"Valid values: copy, final, check, cleanup"
        )

    return SimpleNamespace(**merged)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Live MySQL DB migration to InnoDB destination "
            "with resumable copy and trigger-based delta sync."
        )
    )

    parser.add_argument(
        "--config",
        default="config.py",
        help="Path to config file. Default: config.py",
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser.parse_args()


def connect(args, db=None):
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=db,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
        read_timeout=3600,
        write_timeout=3600,
    )


def exec_one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
    conn.commit()


def fetch_all(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def fetch_one(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def ensure_dest_db(conn, dest_db):
    exec_one(
        conn,
        f"""
        CREATE DATABASE IF NOT EXISTS {q(dest_db)}
        CHARACTER SET utf8mb4
        COLLATE utf8mb4_unicode_ci
        """,
    )


def ensure_meta_tables(conn, dest_db):
    exec_one(
        conn,
        f"""
        CREATE TABLE IF NOT EXISTS {q(dest_db)}.{q(MIGRATION_STATE_TABLE)} (
            table_name VARCHAR(255) PRIMARY KEY,
            last_pk_json TEXT NULL,
            copied_rows BIGINT UNSIGNED NOT NULL DEFAULT 0,
            status VARCHAR(32) NOT NULL DEFAULT 'new',
            updated_at TIMESTAMP NOT NULL
                DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
        """,
    )

    exec_one(
        conn,
        f"""
        CREATE TABLE IF NOT EXISTS {q(dest_db)}.{q(MIGRATION_CHANGE_TABLE)} (
            id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            table_name VARCHAR(255) NOT NULL,
            op ENUM('I','U','D') NOT NULL,
            pk_json JSON NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            applied TINYINT(1) NOT NULL DEFAULT 0,
            KEY idx_apply (applied, id),
            KEY idx_table (table_name)
        ) ENGINE=InnoDB
        """,
    )


def list_base_tables(conn, source_db):
    rows = fetch_all(
        conn,
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (source_db,),
    )

    return [
        r["table_name"]
        for r in rows
        if r["table_name"] not in {MIGRATION_STATE_TABLE, MIGRATION_CHANGE_TABLE}
    ]


def list_views(conn, source_db):
    rows = fetch_all(
        conn,
        """
        SELECT table_name
        FROM information_schema.views
        WHERE table_schema = %s
        ORDER BY table_name
        """,
        (source_db,),
    )
    return rows


def get_columns(conn, source_db, table):
    rows = fetch_all(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (source_db, table),
    )
    return [r["column_name"] for r in rows]


def get_pk_columns(conn, source_db, table):
    rows = fetch_all(
        conn,
        """
        SELECT column_name
        FROM information_schema.key_column_usage
        WHERE table_schema = %s
          AND table_name = %s
          AND constraint_name = 'PRIMARY'
        ORDER BY ordinal_position
        """,
        (source_db, table),
    )
    return [r["column_name"] for r in rows]

def sanitize_create_table_for_innodb(create_sql: str) -> str:
    """
    Remove or rewrite table options that are valid for MyISAM/old engines
    but invalid or risky when forcing ENGINE=InnoDB.
    """

    # Force InnoDB.
    if re.search(r"ENGINE\s*=\s*\w+", create_sql, flags=re.IGNORECASE):
        create_sql = re.sub(
            r"ENGINE\s*=\s*\w+",
            "ENGINE=InnoDB",
            create_sql,
            flags=re.IGNORECASE,
        )
    else:
        create_sql += " ENGINE=InnoDB"

    # Remove MyISAM/Archive/Merge-specific or old table options.
    remove_patterns = [
        r"\s+ROW_FORMAT\s*=\s*\w+",
        r"\s+PACK_KEYS\s*=\s*(?:0|1|DEFAULT)",
        r"\s+CHECKSUM\s*=\s*(?:0|1)",
        r"\s+DELAY_KEY_WRITE\s*=\s*(?:0|1)",
        r"\s+KEY_BLOCK_SIZE\s*=\s*\d+",
        r"\s+DATA\s+DIRECTORY\s*=\s*'[^']*'",
        r"\s+INDEX\s+DIRECTORY\s*=\s*'[^']*'",
        r"\s+UNION\s*=\s*\([^)]+\)",
        r"\s+INSERT_METHOD\s*=\s*\w+",
        r"\s+RAID_TYPE\s*=\s*\w+",
        r"\s+RAID_CHUNKS\s*=\s*\d+",
        r"\s+RAID_CHUNKSIZE\s*=\s*\d+",
        r"\s+TABLESPACE\s+\S+",
    ]

    for pattern in remove_patterns:
        create_sql = re.sub(pattern, "", create_sql, flags=re.IGNORECASE)

    # Avoid ugly double spaces caused by removals.
    create_sql = re.sub(r"[ \t]+", " ", create_sql)

    return create_sql

def create_dest_table_as_innodb(conn, source_db, dest_db, table):
    row = fetch_one(conn, f"SHOW CREATE TABLE {q(source_db)}.{q(table)}")

    if not row or "Create Table" not in row:
        raise RuntimeError(f"Could not get CREATE TABLE for {table}")

    create_sql = row["Create Table"]

    # Replace:
    #   CREATE TABLE `table`
    # with:
    #   CREATE TABLE IF NOT EXISTS `dest_db`.`table`
    create_sql = re.sub(
        rf"CREATE TABLE {re.escape(q(table))}",
        f"CREATE TABLE IF NOT EXISTS {q(dest_db)}.{q(table)}",
        create_sql,
        count=1,
    )

    create_sql = sanitize_create_table_for_innodb(create_sql)

    try:
        exec_one(conn, create_sql)
    except pymysql.err.OperationalError as e:
        print("")
        print(f"[ERROR] Failed to create destination table: {table}")
        print(f"[ERROR] MySQL error: {e}")
        print("")
        print("[DEBUG] Sanitized CREATE TABLE statement was:")
        print(create_sql)
        print("")
        raise


def get_state(conn, dest_db, table):
    row = fetch_one(
        conn,
        f"""
        SELECT last_pk_json, copied_rows, status
        FROM {q(dest_db)}.{q(MIGRATION_STATE_TABLE)}
        WHERE table_name = %s
        """,
        (table,),
    )

    if not row:
        exec_one(
            conn,
            f"""
            INSERT INTO {q(dest_db)}.{q(MIGRATION_STATE_TABLE)}
                (table_name, last_pk_json, copied_rows, status)
            VALUES (%s, NULL, 0, 'new')
            """,
            (table,),
        )
        return None, 0, "new"

    last_pk = json.loads(row["last_pk_json"]) if row["last_pk_json"] else None
    return last_pk, int(row["copied_rows"]), row["status"]


def update_state(conn, dest_db, table, last_pk, copied_rows, status):
    exec_one(
        conn,
        f"""
        UPDATE {q(dest_db)}.{q(MIGRATION_STATE_TABLE)}
        SET last_pk_json = %s,
            copied_rows = copied_rows + %s,
            status = %s
        WHERE table_name = %s
        """,
        (
            json.dumps(last_pk, default=str) if last_pk is not None else None,
            copied_rows,
            status,
            table,
        ),
    )


def reset_state(conn, dest_db, table):
    exec_one(
        conn,
        f"""
        UPDATE {q(dest_db)}.{q(MIGRATION_STATE_TABLE)}
        SET last_pk_json = NULL,
            copied_rows = 0,
            status = 'new'
        WHERE table_name = %s
        """,
        (table,),
    )


def make_trigger_name(table, suffix):
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", table)
    base = f"mig_{safe}"[:48]
    return f"{base}_{suffix}"


def create_change_triggers(conn, source_db, dest_db, table, pk_cols):
    if not pk_cols:
        print(f"[WARN] {table}: no primary key, skipping live change triggers")
        return

    trg_i = make_trigger_name(table, "ai")
    trg_u = make_trigger_name(table, "au")
    trg_d = make_trigger_name(table, "ad")

    for trg in [trg_i, trg_u, trg_d]:
        exec_one(conn, f"DROP TRIGGER IF EXISTS {q(source_db)}.{q(trg)}")

    new_pk_json_args = []
    old_pk_json_args = []

    for col in pk_cols:
        new_pk_json_args.append(f"'{col}'")
        new_pk_json_args.append(f"NEW.{q(col)}")
        old_pk_json_args.append(f"'{col}'")
        old_pk_json_args.append(f"OLD.{q(col)}")

    new_json = "JSON_OBJECT(" + ", ".join(new_pk_json_args) + ")"
    old_json = "JSON_OBJECT(" + ", ".join(old_pk_json_args) + ")"

    exec_one(
        conn,
        f"""
        CREATE TRIGGER {q(source_db)}.{q(trg_i)}
        AFTER INSERT ON {q(source_db)}.{q(table)}
        FOR EACH ROW
        INSERT INTO {q(dest_db)}.{q(MIGRATION_CHANGE_TABLE)}
            (table_name, op, pk_json)
        VALUES
            (%s, 'I', {new_json})
        """,
        (table,),
    )

    exec_one(
        conn,
        f"""
        CREATE TRIGGER {q(source_db)}.{q(trg_u)}
        AFTER UPDATE ON {q(source_db)}.{q(table)}
        FOR EACH ROW
        INSERT INTO {q(dest_db)}.{q(MIGRATION_CHANGE_TABLE)}
            (table_name, op, pk_json)
        VALUES
            (%s, 'U', {new_json})
        """,
        (table,),
    )

    exec_one(
        conn,
        f"""
        CREATE TRIGGER {q(source_db)}.{q(trg_d)}
        AFTER DELETE ON {q(source_db)}.{q(table)}
        FOR EACH ROW
        INSERT INTO {q(dest_db)}.{q(MIGRATION_CHANGE_TABLE)}
            (table_name, op, pk_json)
        VALUES
            (%s, 'D', {old_json})
        """,
        (table,),
    )

    print(f"[TRIGGER] {table}: migration triggers active")


def drop_change_triggers(conn, source_db, table):
    for suffix in ["ai", "au", "ad"]:
        trg = make_trigger_name(table, suffix)
        exec_one(conn, f"DROP TRIGGER IF EXISTS {q(source_db)}.{q(trg)}")


def list_migration_triggers(conn, source_db):
    """
    Return every migration trigger (mig_*) currently defined on the source
    schema, regardless of whether the matching table still exists. Used by
    cleanup mode to remove orphaned triggers that the per-table drop misses.
    """
    rows = fetch_all(
        conn,
        r"""
        SELECT trigger_name
        FROM information_schema.triggers
        WHERE trigger_schema = %s
          AND trigger_name LIKE 'mig\_%'
        ORDER BY trigger_name
        """,
        (source_db,),
    )
    return [r["trigger_name"] for r in rows]


def build_pk_where_after(pk_cols, last_pk):
    if not last_pk:
        return "", []

    cols = ", ".join(q(c) for c in pk_cols)
    placeholders = ", ".join(["%s"] * len(pk_cols))
    values = [last_pk[c] for c in pk_cols]

    return f"WHERE ({cols}) > ({placeholders})", values


def insert_rows(conn, dest_db, table, columns, rows):
    if not rows:
        return

    col_sql = ", ".join(q(c) for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))

    update_sql = ", ".join(
        f"{q(c)} = VALUES({q(c)})"
        for c in columns
    )

    sql = f"""
        INSERT INTO {q(dest_db)}.{q(table)}
            ({col_sql})
        VALUES
            ({placeholders})
        ON DUPLICATE KEY UPDATE
            {update_sql}
    """

    values = []
    for row in rows:
        values.append(tuple(row[c] for c in columns))

    with conn.cursor() as cur:
        cur.executemany(sql, values)

    conn.commit()


def copy_table_chunked(
    conn,
    source_db,
    dest_db,
    table,
    chunk_size,
    sleep_seconds,
    full_rescan=False,
):
    columns = get_columns(conn, source_db, table)
    pk_cols = get_pk_columns(conn, source_db, table)

    if not columns:
        print(f"[SKIP] {table}: no columns")
        return

    if not pk_cols:
        print(
            f"[WARN] {table}: no primary key. "
            f"Live idempotent copy is not reliable for this table."
        )
        copy_table_without_pk(conn, source_db, dest_db, table, columns)
        return

    if full_rescan:
        reset_state(conn, dest_db, table)

    last_pk, _, status = get_state(conn, dest_db, table)

    if status == "done" and not full_rescan:
        print(f"[OK] {table}: already copied")
        return

    print(f"[COPY] {table}: starting from PK {last_pk}")

    order_sql = ", ".join(q(c) for c in pk_cols)
    col_sql = ", ".join(q(c) for c in columns)

    while True:
        where_sql, params = build_pk_where_after(pk_cols, last_pk)

        sql = f"""
            SELECT {col_sql}
            FROM {q(source_db)}.{q(table)}
            {where_sql}
            ORDER BY {order_sql}
            LIMIT %s
        """

        rows = fetch_all(conn, sql, tuple(params + [chunk_size]))

        if not rows:
            update_state(conn, dest_db, table, last_pk, 0, "done")
            print(f"[DONE] {table}")
            break

        insert_rows(conn, dest_db, table, columns, rows)

        last_row = rows[-1]
        last_pk = {c: last_row[c] for c in pk_cols}

        update_state(conn, dest_db, table, last_pk, len(rows), "copying")

        print(f"[COPY] {table}: +{len(rows)} rows, last_pk={last_pk}")

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


def copy_table_without_pk(conn, source_db, dest_db, table, columns):
    col_sql = ", ".join(q(c) for c in columns)

    dest_count = fetch_one(
        conn,
        f"SELECT COUNT(*) AS c FROM {q(dest_db)}.{q(table)}",
    )["c"]

    if dest_count > 0:
        print(
            f"[SKIP] {table}: destination already has {dest_count} rows. "
            f"No-PK table skipped to avoid duplicates."
        )
        return

    sql = f"""
        INSERT INTO {q(dest_db)}.{q(table)}
            ({col_sql})
        SELECT {col_sql}
        FROM {q(source_db)}.{q(table)}
    """

    exec_one(conn, sql)

    exec_one(
        conn,
        f"""
        INSERT INTO {q(dest_db)}.{q(MIGRATION_STATE_TABLE)}
            (table_name, last_pk_json, copied_rows, status)
        VALUES
            (%s, NULL, ROW_COUNT(), 'done')
        ON DUPLICATE KEY UPDATE
            status = 'done'
        """,
        (table,),
    )

    print(f"[DONE] {table}: copied without PK")


def apply_change_row(conn, source_db, dest_db, change):
    table = change["table_name"]
    op = change["op"]
    pk_json = change["pk_json"]

    pk = json.loads(pk_json) if isinstance(pk_json, str) else pk_json

    pk_cols = list(pk.keys())
    columns = get_columns(conn, source_db, table)

    if not pk_cols:
        print(f"[WARN] Change #{change['id']} for {table}: no PK data")
        return

    where_sql = " AND ".join(f"{q(c)} = %s" for c in pk_cols)
    where_values = [pk[c] for c in pk_cols]

    if op == "D":
        exec_one(
            conn,
            f"""
            DELETE FROM {q(dest_db)}.{q(table)}
            WHERE {where_sql}
            """,
            where_values,
        )
    else:
        col_sql = ", ".join(q(c) for c in columns)

        row = fetch_one(
            conn,
            f"""
            SELECT {col_sql}
            FROM {q(source_db)}.{q(table)}
            WHERE {where_sql}
            LIMIT 1
            """,
            where_values,
        )

        if row:
            insert_rows(conn, dest_db, table, columns, [row])
        else:
            exec_one(
                conn,
                f"""
                DELETE FROM {q(dest_db)}.{q(table)}
                WHERE {where_sql}
                """,
                where_values,
            )

    exec_one(
        conn,
        f"""
        UPDATE {q(dest_db)}.{q(MIGRATION_CHANGE_TABLE)}
        SET applied = 1
        WHERE id = %s
        """,
        (change["id"],),
    )


def apply_changes(conn, source_db, dest_db, batch_size):
    print("[FINAL] Applying captured changes")

    while True:
        changes = fetch_all(
            conn,
            f"""
            SELECT
                id,
                table_name,
                op,
                CAST(pk_json AS CHAR) AS pk_json
            FROM {q(dest_db)}.{q(MIGRATION_CHANGE_TABLE)}
            WHERE applied = 0
            ORDER BY id
            LIMIT %s
            """,
            (batch_size,),
        )

        if not changes:
            break

        for change in changes:
            apply_change_row(conn, source_db, dest_db, change)

        print(f"[FINAL] Applied {len(changes)} changes")

    print("[FINAL] Change log fully applied")


def create_or_replace_views(conn, source_db, dest_db):
    views = list_views(conn, source_db)

    if not views:
        print("[VIEW] No views found")
        return

    for view in views:
        name = view["table_name"]

        row = fetch_one(conn, f"SHOW CREATE VIEW {q(source_db)}.{q(name)}")

        if not row or "Create View" not in row:
            print(f"[WARN] View {name}: could not read definition")
            continue

        create_sql = row["Create View"]

        create_sql = re.sub(
            r"CREATE\s+.*?\s+VIEW",
            "CREATE OR REPLACE SQL SECURITY INVOKER VIEW",
            create_sql,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )

        create_sql = create_sql.replace(f"`{source_db}`.", f"`{dest_db}`.")

        create_sql = re.sub(
            rf"VIEW\s+{re.escape(q(source_db))}\.{re.escape(q(name))}",
            f"VIEW {q(dest_db)}.{q(name)}",
            create_sql,
            count=1,
            flags=re.IGNORECASE,
        )

        create_sql = re.sub(
            rf"VIEW\s+{re.escape(q(name))}",
            f"VIEW {q(dest_db)}.{q(name)}",
            create_sql,
            count=1,
            flags=re.IGNORECASE,
        )

        try:
            exec_one(conn, create_sql)
            print(f"[VIEW] {name}: created/replaced")
        except Exception as e:
            print(f"[WARN] View {name} failed: {e}")


			
def check_counts(conn, source_db, dest_db):
    tables = list_base_tables(conn, source_db)

    print("")
    print("Table row count comparison")
    print("--------------------------")

    differences = 0

    for table in tables:
        try:
            src = fetch_one(
                conn,
                f"SELECT COUNT(*) AS c FROM {q(source_db)}.{q(table)}",
            )["c"]

            dst = fetch_one(
                conn,
                f"SELECT COUNT(*) AS c FROM {q(dest_db)}.{q(table)}",
            )["c"]

            status = "OK" if src == dst else "DIFF"

            if status == "DIFF":
                differences += 1

            print(f"{status:4} {table:60} source={src} dest={dst}")

        except Exception as e:
            differences += 1
            print(f"ERR  {table:60} {e}")

    print("")
    if differences == 0:
        print("[CHECK] Row counts match")
    else:
        print(f"[CHECK] {differences} table(s) differ")


def show_no_pk_tables(conn, source_db):
    tables = list_base_tables(conn, source_db)
    no_pk = []

    for table in tables:
        if not get_pk_columns(conn, source_db, table):
            no_pk.append(table)

    if no_pk:
        print("")
        print("[WARN] Tables without primary key:")
        for table in no_pk:
            print(f"  - {table}")
        print("")
        print(
            "[WARN] These tables cannot be safely live-synced for updates/deletes. "
            "Add a primary key or handle them during downtime."
        )
        print("")


def run_copy(args):
    conn = connect(args)

    ensure_dest_db(conn, args.dest_db)
    ensure_meta_tables(conn, args.dest_db)

    tables = list_base_tables(conn, args.source_db)

    print(f"[INFO] Found {len(tables)} base tables")
    show_no_pk_tables(conn, args.source_db)

    exec_one(conn, "SET SESSION foreign_key_checks = 0")
    exec_one(conn, "SET SESSION unique_checks = 0")

    for table in tables:
        print("")
        print(f"[TABLE] {table}")

        create_dest_table_as_innodb(conn, args.source_db, args.dest_db, table)

        pk_cols = get_pk_columns(conn, args.source_db, table)

        create_change_triggers(
            conn,
            args.source_db,
            args.dest_db,
            table,
            pk_cols,
        )

        copy_table_chunked(
            conn,
            args.source_db,
            args.dest_db,
            table,
            args.chunk_size,
            args.sleep,
            full_rescan=args.full_rescan,
        )

    create_or_replace_views(conn, args.source_db, args.dest_db)

    exec_one(conn, "SET SESSION unique_checks = 1")
    exec_one(conn, "SET SESSION foreign_key_checks = 1")

    conn.close()


def run_final(args):
    conn = connect(args)

    ensure_dest_db(conn, args.dest_db)
    ensure_meta_tables(conn, args.dest_db)

    exec_one(conn, "SET SESSION foreign_key_checks = 0")
    exec_one(conn, "SET SESSION unique_checks = 0")

    apply_changes(conn, args.source_db, args.dest_db, args.change_batch_size)
    create_or_replace_views(conn, args.source_db, args.dest_db)

    if args.drop_triggers:
        for table in list_base_tables(conn, args.source_db):
            drop_change_triggers(conn, args.source_db, table)
        print("[FINAL] Migration triggers dropped")

    exec_one(conn, "SET SESSION unique_checks = 1")
    exec_one(conn, "SET SESSION foreign_key_checks = 1")

    conn.close()


def run_cleanup(args):
    conn = connect(args)

    triggers = list_migration_triggers(conn, args.source_db)

    if not triggers:
        print(f"[CLEANUP] No migration triggers found in {args.source_db}")
        conn.close()
        return

    print(f"[CLEANUP] Found {len(triggers)} migration trigger(s) in {args.source_db}")

    for trg in triggers:
        exec_one(conn, f"DROP TRIGGER IF EXISTS {q(args.source_db)}.{q(trg)}")
        print(f"[CLEANUP] Dropped {trg}")

    print(f"[CLEANUP] Removed {len(triggers)} migration trigger(s)")

    conn.close()


def main():
    cli_args = parse_args()
    args = load_config_file(cli_args.config)

    started = datetime.now()

    print(f"[START] {started.isoformat(timespec='seconds')}")
    print(f"[VERSION] {__version__}")
    print(f"[CONFIG] {cli_args.config}")
    print(f"[MODE] {args.mode}")
    print(f"[SOURCE] {args.source_db}")
    print(f"[DEST] {args.dest_db}")
    print("")

    try:
        if args.mode == "copy":
            run_copy(args)

        elif args.mode == "final":
            run_final(args)

        elif args.mode == "check":
            conn = connect(args)
            check_counts(conn, args.source_db, args.dest_db)
            conn.close()

        elif args.mode == "cleanup":
            run_cleanup(args)

        else:
            raise RuntimeError("Unknown mode")

    except KeyboardInterrupt:
        print("")
        print("[STOP] Interrupted. Re-run the same command to continue.")
        sys.exit(130)

    ended = datetime.now()

    print("")
    print(f"[END] {ended.isoformat(timespec='seconds')}")
    print(f"[DURATION] {ended - started}")


if __name__ == "__main__":
    main()