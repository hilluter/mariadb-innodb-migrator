# config.check.example.py

MYSQL = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "PASSWORD",
    "source_db": "old_db",
    "dest_db": "new_db",
}

MIGRATION = {
    "mode": "check",
}