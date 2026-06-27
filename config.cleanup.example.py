# config.cleanup.example.py
#
# Removes ALL migration triggers (mig_*) from the source schema, including
# orphaned ones whose table was dropped or renamed. Use this if a migration
# was abandoned, or before tearing down the destination database, to make sure
# no trigger is left writing into dest_db.__migration_changes.

MYSQL = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "PASSWORD",
    "source_db": "old_db",
    "dest_db": "new_db",
}

MIGRATION = {
    "mode": "cleanup",
}
