import argparse
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values

REQUIRED_SUFFIXES = ["ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "BUCKET"]
SOURCE_PREFIX = "SOURCE_S3"
DESTINATION_PREFIX = "DESTINATION_S3"
DEFAULT_REGION = "auto"


def load_side_settings(config, prefix):
    settings = {suffix: config.get(f"{prefix}_{suffix}") for suffix in REQUIRED_SUFFIXES}
    missing = [f"{prefix}_{suffix}" for suffix, value in settings.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required config values: {', '.join(missing)}")

    settings["ENDPOINT_URL"] = (config.get(f"{prefix}_ENDPOINT_URL") or "").strip() or None
    settings["REGION"] = (config.get(f"{prefix}_REGION") or "").strip() or DEFAULT_REGION
    settings["KEY_PREFIX"] = (config.get(f"{prefix}_PREFIX") or "").strip().strip("/")
    return settings


def load_settings(config_path):
    config = dotenv_values(config_path)
    if not config:
        raise RuntimeError(f"Config file not found or empty: {config_path}")

    return {
        "source": load_side_settings(config, SOURCE_PREFIX),
        "destination": load_side_settings(config, DESTINATION_PREFIX),
    }


def build_client(settings):
    return boto3.client(
        "s3",
        aws_access_key_id=settings["ACCESS_KEY_ID"],
        aws_secret_access_key=settings["SECRET_ACCESS_KEY"],
        endpoint_url=settings["ENDPOINT_URL"],
        region_name=settings["REGION"],
        config=Config(signature_version="s3v4"),
    )


def list_objects(client, bucket, prefix):
    paginator = client.get_paginator("list_objects_v2")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix

    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents", []):
            yield obj


def destination_key(key, source_prefix, destination_prefix):
    relative = key[len(source_prefix):].lstrip("/") if source_prefix else key
    return f"{destination_prefix}/{relative}" if destination_prefix else relative


def sync_buckets(source_settings, destination_settings):
    source_client = build_client(source_settings)
    destination_client = build_client(destination_settings)

    source_bucket = source_settings["BUCKET"]
    destination_bucket = destination_settings["BUCKET"]
    source_prefix = source_settings["KEY_PREFIX"]
    destination_prefix = destination_settings["KEY_PREFIX"]

    objects = list(list_objects(source_client, source_bucket, source_prefix))
    if not objects:
        print(f"Nothing to sync: s3://{source_bucket}/{source_prefix} has no objects.")
        return 0

    print(f"Syncing {len(objects)} object(s) from s3://{source_bucket}/{source_prefix} "
          f"to s3://{destination_bucket}/{destination_prefix}...")

    failures = 0
    for obj in objects:
        key = obj["Key"]
        dest_key = destination_key(key, source_prefix, destination_prefix)
        size_mb = obj["Size"] / (1024 * 1024)

        try:
            response = source_client.get_object(Bucket=source_bucket, Key=key)
            destination_client.upload_fileobj(response["Body"], destination_bucket, dest_key)
            print(f"  synced {key} ({size_mb:.1f} MB) -> s3://{destination_bucket}/{dest_key}")
        except (BotoCoreError, ClientError) as error:
            failures += 1
            print(f"  FAILED {key}: {error}", file=sys.stderr)

    if failures:
        print(f"Done with {failures} failure(s) out of {len(objects)}.", file=sys.stderr)
    else:
        print(f"Success! {len(objects)} object(s) synced.")

    return failures


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy objects from one S3 bucket to another using settings from a config file."
    )
    parser.add_argument(
        "config",
        help="Path to the config file with SOURCE_S3_* and DESTINATION_S3_* settings",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        settings = load_settings(args.config)
    except RuntimeError as error:
        print(error, file=sys.stderr)
        sys.exit(1)

    if sync_buckets(settings["source"], settings["destination"]):
        sys.exit(1)
