# config.final.example.py

MYSQL = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "PASSWORD",
    "source_db": "old_db",
    "dest_db": "new_db",
}

MIGRATION = {
    "mode": "final",
    "chunk_size": 1000,
    "sleep": 0.20,
    "change_batch_size": 1000,
    "full_rescan": False,
    "drop_triggers": True,
}