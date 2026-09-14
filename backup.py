import os
import shutil
import subprocess
import sys
from datetime import datetime

import psycopg2
from dotenv import load_dotenv

REQUIRED_VARS = ["db_host", "db_port", "db_user", "db_password", "db_name"]
EXCLUDE_TABLES_VAR = "EXCLUDE_TABLES"
DUMP_PATH_VAR = "DUMP_PATH"
DEFAULT_DUMP_PATH = "./dumps"
BACKUP_PREFIX = "backup"


def load_settings():
    load_dotenv()

    settings = {name: os.environ.get(name) for name in REQUIRED_VARS}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    raw_excludes = os.environ.get(EXCLUDE_TABLES_VAR, "")
    settings[EXCLUDE_TABLES_VAR] = [
        table.strip() for table in raw_excludes.split(",") if table.strip()
    ]

    dump_path = os.environ.get(DUMP_PATH_VAR, "").strip() or DEFAULT_DUMP_PATH
    try:
        os.makedirs(dump_path, exist_ok=True)
    except OSError as error:
        raise RuntimeError(
            f"{DUMP_PATH_VAR} is not a usable directory ({dump_path}): {error}"
        ) from error
    settings[DUMP_PATH_VAR] = dump_path

    return settings


def check_connection(settings):
    print(f"Connecting to {settings['db_name']} on {settings['db_host']}:{settings['db_port']}...")
    try:
        connection = psycopg2.connect(
            host=settings["db_host"],
            port=settings["db_port"],
            user=settings["db_user"],
            password=settings["db_password"],
            dbname=settings["db_name"],
        )
    except psycopg2.Error as error:
        raise RuntimeError(f"Could not connect to the database: {error}") from error

    connection.close()
    print("Connection OK.")


def backup_postgres_db(settings):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    output_file = os.path.join(
        settings[DUMP_PATH_VAR], f"{BACKUP_PREFIX}-{timestamp}.sql"
    )
    latest_file = os.path.join(settings[DUMP_PATH_VAR], "latest.sql")

    command = [
        "pg_dump",
        "-h", settings["db_host"],
        "-p", settings["db_port"],
        "-U", settings["db_user"],
        "-F", "p",
    ]

    for table in settings[EXCLUDE_TABLES_VAR]:
        command.append(f"--exclude-table-data={table}")

    command += ["-f", output_file, settings["db_name"]]

    env_vars = os.environ.copy()
    env_vars["PGPASSWORD"] = settings["db_password"]

    try:
        print(f"Starting backup for {settings['db_name']}...")
        if settings[EXCLUDE_TABLES_VAR]:
            print(f"Excluding data from: {', '.join(settings[EXCLUDE_TABLES_VAR])}")
        result = subprocess.run(
            command,
            env=env_vars,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Success! Backup saved to {output_file}")
        shutil.copy(output_file, latest_file)
        print(f"Backup copied as {latest_file}")

    except subprocess.CalledProcessError as error:
        print("Backup failed!")
        print(f"Error Message: {error.stderr}")

def backup_postgres_db_full(settings):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    output_file = os.path.join(
        settings[DUMP_PATH_VAR], f"{BACKUP_PREFIX}-{timestamp}-FULL.sql"
    )
    latest_file = os.path.join(settings[DUMP_PATH_VAR], "latest-FULL.sql")

    command = [
        "pg_dump",
        "-h", settings["db_host"],
        "-p", settings["db_port"],
        "-U", settings["db_user"],
        "-F", "p",
    ]

    command += ["-f", output_file, settings["db_name"]]

    env_vars = os.environ.copy()
    env_vars["PGPASSWORD"] = settings["db_password"]

    try:
        print(f"Starting FULL backup for {settings['db_name']}...")
        result = subprocess.run(
            command,
            env=env_vars,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Success! FULL Backup saved to {output_file}")
        shutil.copy(output_file, latest_file)
        print(f"FULL Backup copied as {latest_file}")

    except subprocess.CalledProcessError as error:
        print("Backup failed!")
        print(f"Error Message: {error.stderr}")

if __name__ == "__main__":
    try:
        settings = load_settings()
        check_connection(settings)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        sys.exit(1)

    backup_postgres_db(settings)
    backup_postgres_db_full(settings)
