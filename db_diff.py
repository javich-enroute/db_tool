import argparse
import os
import re
import sys
from datetime import datetime

import psycopg2
from dotenv import dotenv_values
from psycopg2 import sql

REQUIRED_SUFFIXES = ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]
RAILWAY_PREFIX = "RAILWAY"
RDS_PREFIX = "RDS"
SCHEMA = "public"
REPORT_PREFIX = "db_diff"
CONNECT_TIMEOUT_SECONDS = 15

# Column types (with any precision such as "(3)" removed) that hold date values.
DATE_TYPES = {"date", "timestamp without time zone", "timestamp with time zone"}

MISSING_IN_RDS = "Missing in RDS"
MISSING_IN_RAILWAY = "Missing in RAILWAY"
TYPE_MISMATCH = "Type mismatch"

# Ordinary ('r') and partitioned ('p') tables only; views are not compared.
TABLES_QUERY = """
    SELECT c.relname
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s
      AND c.relkind IN ('r', 'p')
    ORDER BY c.relname
"""

COLUMNS_QUERY = """
    SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s
      AND c.relkind IN ('r', 'p')
      AND a.attnum > 0
      AND NOT a.attisdropped
    ORDER BY c.relname, a.attnum
"""


# ---------------------------------------------------------------------------
# Config and connections
# ---------------------------------------------------------------------------

def load_side_settings(config, prefix):
    settings = {suffix: (config.get(f"{prefix}_{suffix}") or "").strip() for suffix in REQUIRED_SUFFIXES}
    missing = [f"{prefix}_{suffix}" for suffix, value in settings.items() if not value]

    settings["DB_SSLMODE"] = (config.get(f"{prefix}_DB_SSLMODE") or "").strip() or None
    settings["LABEL"] = prefix
    return settings, missing


def load_settings(config_path):
    if not os.path.isfile(config_path):
        raise RuntimeError(f"Config file not found: {config_path}")

    config = dotenv_values(config_path)
    railway, railway_missing = load_side_settings(config, RAILWAY_PREFIX)
    rds, rds_missing = load_side_settings(config, RDS_PREFIX)

    missing = railway_missing + rds_missing
    if missing:
        raise RuntimeError(
            f"Missing required config values in {config_path}: {', '.join(missing)}"
        )

    return {"railway": railway, "rds": rds}


def describe(settings):
    return f"{settings['DB_HOST']}:{settings['DB_PORT']}/{settings['DB_NAME']}"


def connect(settings):
    label = settings["LABEL"]
    print(f"Connecting to {label} ({describe(settings)})...")

    params = {
        "host": settings["DB_HOST"],
        "port": settings["DB_PORT"],
        "user": settings["DB_USER"],
        "password": settings["DB_PASSWORD"],
        "dbname": settings["DB_NAME"],
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
    }
    if settings["DB_SSLMODE"]:
        params["sslmode"] = settings["DB_SSLMODE"]

    try:
        connection = psycopg2.connect(**params)
    except psycopg2.Error as error:
        raise RuntimeError(f"Could not connect to {label}: {error}") from error

    # Read-only protects both databases; autocommit keeps one failed query
    # (e.g. a permissions error on a single table) from aborting the rest.
    connection.set_session(readonly=True, autocommit=True)
    print(f"{label} connection OK.")
    return connection


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def list_tables(connection):
    with connection.cursor() as cursor:
        cursor.execute(TABLES_QUERY, (SCHEMA,))
        return [row[0] for row in cursor.fetchall()]


def get_columns(connection):
    """Return {table: {column: type}} for every table in SCHEMA, columns in table order."""
    columns = {}
    with connection.cursor() as cursor:
        cursor.execute(COLUMNS_QUERY, (SCHEMA,))
        for table, column, column_type in cursor.fetchall():
            columns.setdefault(table, {})[column] = column_type
    return columns


def count_rows(connection, table):
    """Return (count, None) on success or (None, error message) on failure."""
    query = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
        sql.Identifier(SCHEMA), sql.Identifier(table)
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(query)
            return cursor.fetchone()[0], None
    except psycopg2.Error as error:
        return None, str(error).strip().splitlines()[0]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_tables(railway_tables, rds_tables):
    """Walk the RAILWAY tables and split them by whether they also exist in RDS."""
    rds_set = set(rds_tables)
    railway_set = set(railway_tables)

    common = []
    railway_only = []
    for table in railway_tables:
        if table in rds_set:
            common.append(table)
        else:
            railway_only.append(table)

    rds_only = [table for table in rds_tables if table not in railway_set]
    return common, railway_only, rds_only


def is_date_type(column_type):
    return re.sub(r"\(\d+\)", "", column_type) in DATE_TYPES


def compare_fields(railway_columns, rds_columns):
    """Return (railway_field_diffs, rds_field_diffs).

    railway_field_diffs: fields only in RAILWAY or with a different type.
    rds_field_diffs: fields only in RDS or with a different type.
    Type mismatches appear in both lists, as the same entry.
    """
    railway_field_diffs = []
    rds_field_diffs = []

    for field, railway_type in railway_columns.items():
        rds_type = rds_columns.get(field)
        if rds_type is None:
            railway_field_diffs.append(
                {"field": field, "railway_type": railway_type, "rds_type": None,
                 "difference": MISSING_IN_RDS}
            )
        elif rds_type != railway_type:
            mismatch = {"field": field, "railway_type": railway_type, "rds_type": rds_type,
                        "difference": TYPE_MISMATCH}
            railway_field_diffs.append(mismatch)
            rds_field_diffs.append(mismatch)

    for field, rds_type in rds_columns.items():
        if field not in railway_columns:
            rds_field_diffs.append(
                {"field": field, "railway_type": None, "rds_type": rds_type,
                 "difference": MISSING_IN_RAILWAY}
            )

    return railway_field_diffs, rds_field_diffs


def analyze_tables(railway_connection, rds_connection, railway_tables, rds_tables):
    railway_columns = get_columns(railway_connection)
    rds_columns = get_columns(rds_connection)
    rds_set = set(rds_tables)

    results = []
    total = len(railway_tables)
    for index, table in enumerate(railway_tables, start=1):
        in_rds = table in rds_set
        columns = railway_columns.get(table, {})

        railway_count, railway_error = count_rows(railway_connection, table)
        rds_count, rds_error = count_rows(rds_connection, table) if in_rds else (None, None)

        date_fields = [(field, column_type) for field, column_type in columns.items()
                       if is_date_type(column_type)]

        if in_rds:
            railway_field_diffs, rds_field_diffs = compare_fields(columns, rds_columns.get(table, {}))
        else:
            railway_field_diffs, rds_field_diffs = [], []

        results.append({
            "table": table,
            "in_rds": in_rds,
            "railway_count": railway_count,
            "railway_error": railway_error,
            "rds_count": rds_count,
            "rds_error": rds_error,
            "date_fields": date_fields,
            "railway_field_diffs": railway_field_diffs,
            "rds_field_diffs": rds_field_diffs,
        })

        progress = f"RAILWAY {format_count(railway_count, railway_error)}"
        if in_rds:
            progress += f", RDS {format_count(rds_count, rds_error)}"
        else:
            progress += ", missing in RDS"
        print(f"  [{index}/{total}] {table} ... {progress}")

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def md(text):
    return str(text).replace("|", "\\|")


def format_count(count, error):
    if error:
        return "error"
    return f"{count:,}"


def format_difference(result):
    if not result["in_rds"] or result["railway_count"] is None or result["rds_count"] is None:
        return "n/a"
    difference = result["rds_count"] - result["railway_count"]
    return "0" if difference == 0 else f"{difference:+,}"


def merged_field_diffs(result):
    """Combine both diff lists, keeping each type mismatch once."""
    return result["railway_field_diffs"] + [
        diff for diff in result["rds_field_diffs"] if diff["difference"] != TYPE_MISMATCH
    ]


def counts_differ(result):
    return (result["in_rds"]
            and result["railway_count"] is not None
            and result["rds_count"] is not None
            and result["railway_count"] != result["rds_count"])


def build_report(settings, railway_tables, rds_tables, common, railway_only, rds_only, results):
    tables_with_field_diffs = [result for result in results if merged_field_diffs(result)]
    tables_with_count_diffs = [result for result in results if counts_differ(result)]
    count_errors = [result for result in results if result["railway_error"] or result["rds_error"]]

    lines = [
        "# Database diff report: RAILWAY vs RDS",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Schema: `{SCHEMA}`",
        "",
        f"- **RAILWAY:** {describe(settings['railway'])}",
        f"- **RDS:** {describe(settings['rds'])}",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Tables in RAILWAY | {len(railway_tables)} |",
        f"| Tables in RDS | {len(rds_tables)} |",
        f"| Tables in both | {len(common)} |",
        f"| Tables only in RAILWAY (`railway_only`) | {len(railway_only)} |",
        f"| Tables only in RDS (`rds_only`) | {len(rds_only)} |",
        f"| Shared tables with different record counts | {len(tables_with_count_diffs)} |",
        f"| Shared tables with field differences | {len(tables_with_field_diffs)} |",
    ]
    if count_errors:
        lines.append(f"| Tables where a record count failed | {len(count_errors)} |")

    lines += ["", "## Tables only in RAILWAY (`railway_only`)", ""]
    lines += [f"- {md(table)}" for table in railway_only] or ["None."]

    lines += ["", "## Tables only in RDS (`rds_only`)", ""]
    lines += [f"- {md(table)}" for table in rds_only] or ["None."]

    lines += [
        "",
        "## Record counts",
        "",
        "Difference is RDS minus RAILWAY. A negative number means RDS has fewer rows.",
        "",
        "| Table | RAILWAY rows | RDS rows | Difference |",
        "| --- | ---: | ---: | ---: |",
    ]
    for result in results:
        rds_rows = (format_count(result["rds_count"], result["rds_error"])
                    if result["in_rds"] else "n/a (missing in RDS)")
        lines.append(
            f"| {md(result['table'])} "
            f"| {format_count(result['railway_count'], result['railway_error'])} "
            f"| {rds_rows} | {format_difference(result)} |"
        )

    if count_errors:
        lines += ["", "### Record count errors", ""]
        for result in count_errors:
            if result["railway_error"]:
                lines.append(f"- {md(result['table'])} (RAILWAY): {md(result['railway_error'])}")
            if result["rds_error"]:
                lines.append(f"- {md(result['table'])} (RDS): {md(result['rds_error'])}")

    lines += [
        "",
        "## Date fields (RAILWAY)",
        "",
        "Columns of type date, timestamp or timestamp with time zone.",
        "",
        "| Table | Date fields |",
        "| --- | --- |",
    ]
    for result in results:
        fields = ", ".join(f"{md(field)} ({md(column_type)})"
                           for field, column_type in result["date_fields"])
        lines.append(f"| {md(result['table'])} | {fields or '_none_'} |")

    lines += ["", "## Field differences", ""]
    if tables_with_field_diffs:
        lines += [
            "Only tables present in both databases are compared.",
            "",
            "| Table | Field | RAILWAY type | RDS type | Difference |",
            "| --- | --- | --- | --- | --- |",
        ]
        for result in tables_with_field_diffs:
            for diff in merged_field_diffs(result):
                lines.append(
                    f"| {md(result['table'])} | {md(diff['field'])} "
                    f"| {md(diff['railway_type'] or 'n/a')} | {md(diff['rds_type'] or 'n/a')} "
                    f"| {diff['difference']} |"
                )
    else:
        lines.append("No field differences found in tables present in both databases.")

    lines.append("")
    return "\n".join(lines)


def write_report(report, output_path):
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as report_file:
        report_file.write(report)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(settings, output_path):
    railway_connection = None
    rds_connection = None
    try:
        railway_connection = connect(settings["railway"])
        railway_tables = list_tables(railway_connection)
        print(f"RAILWAY has {len(railway_tables)} table(s) in schema '{SCHEMA}'.")

        rds_connection = connect(settings["rds"])
        rds_tables = list_tables(rds_connection)
        print(f"RDS has {len(rds_tables)} table(s) in schema '{SCHEMA}'.")

        common, railway_only, rds_only = compare_tables(railway_tables, rds_tables)
        print(f"Tables in both: {len(common)}, railway_only: {len(railway_only)}, "
              f"rds_only: {len(rds_only)}.")

        print("Analyzing RAILWAY tables...")
        results = analyze_tables(railway_connection, rds_connection, railway_tables, rds_tables)
    except psycopg2.Error as error:
        raise RuntimeError(f"Database query failed: {error}") from error
    finally:
        for connection in (railway_connection, rds_connection):
            if connection is not None:
                connection.close()

    report = build_report(settings, railway_tables, rds_tables, common, railway_only, rds_only, results)
    write_report(report, output_path)
    print(f"Success! Report saved to {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compare the public schema of two PostgreSQL databases (RAILWAY vs RDS) "
                    "and write a Markdown report."
    )
    parser.add_argument(
        "config",
        help="Path to the config file with RAILWAY_DB_* and RDS_DB_* settings "
             "(key=value format, see dbdiff.env.example)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Path of the Markdown report (default: ./db_diff-YYYY-MM-DD_HH-MM.md)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    output_path = args.output or f"{REPORT_PREFIX}-{datetime.now().strftime('%Y-%m-%d_%H-%M')}.md"
    try:
        settings = load_settings(args.config)
        run(settings, output_path)
    except (RuntimeError, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
