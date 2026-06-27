# config.example.py

MYSQL = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "PASSWORD",
    "source_db": "old_db",
    "dest_db": "new_db",
}

MIGRATION = {
    # Available modes:
    #   copy  = initial long live copy
    #   final = apply captured changes after stopping the app
    #   check = compare table row counts
    "mode": "copy",

    # Conservative values for live production migration.
    # Increase chunk_size if the server is calm.
    "chunk_size": 1000,

    # Sleep between chunks, in seconds.
    # 0.20 = 200ms pause after each chunk.
    "sleep": 0.20,

    # Used when mode = final
    "change_batch_size": 1000,

    # Usually False.
    # If True, it re-reads PK tables from the beginning using
    # INSERT ... ON DUPLICATE KEY UPDATE.
    "full_rescan": False,

    # Usually False during copy.
    # Set True only for final run after the app is stopped.
    "drop_triggers": False,
}
