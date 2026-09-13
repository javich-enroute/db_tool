import os
import subprocess
import sys

import psycopg2
from dotenv import load_dotenv

REQUIRED_VARS = ["db_host", "db_port", "db_user", "db_password", "db_name"]


def load_settings():
    load_dotenv()

    settings = {name: os.environ.get(name) for name in REQUIRED_VARS}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    return settings


def check_connection(settings):
    # 2. Fail early if the server is unreachable or rejects the credentials
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
    output_file = "latest.sql"

    command = [
        "pg_dump",
        "-h", settings["db_host"],
        "-p", settings["db_port"],
        "-U", settings["db_user"],
        "-F", "p",
        "-f", output_file,
        settings["db_name"]
    ]

    env_vars = os.environ.copy()
    env_vars["PGPASSWORD"] = settings["db_password"]

    try:
        # 5. Run the command safely without using shell=True
        print(f"Starting backup for {settings['db_name']}...")
        result = subprocess.run(
            command,
            env=env_vars,
            check=True,
            capture_output=True,
            text=True
        )
        print(f"Success! Backup saved to {output_file}")

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
