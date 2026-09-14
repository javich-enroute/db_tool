import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

S3_ENV_FILE = ".s3env"
REQUIRED_S3_VARS = ["S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET"]
DUMP_PATH_VAR = "DUMP_PATH"
DEFAULT_DUMP_PATH = "./dumps"
DEFAULT_REGION = "auto"


def load_settings():
    load_dotenv()
    load_dotenv(S3_ENV_FILE)

    settings = {name: os.environ.get(name, "").strip() for name in REQUIRED_S3_VARS}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing required variables in {S3_ENV_FILE}: {', '.join(missing)}"
        )

    settings["S3_ENDPOINT_URL"] = os.environ.get("S3_ENDPOINT_URL", "").strip() or None
    settings["S3_REGION"] = os.environ.get("S3_REGION", "").strip() or DEFAULT_REGION
    settings["S3_PREFIX"] = os.environ.get("S3_PREFIX", "").strip().strip("/")

    dump_path = os.environ.get(DUMP_PATH_VAR, "").strip() or DEFAULT_DUMP_PATH
    if not os.path.isdir(dump_path):
        raise RuntimeError(f"{DUMP_PATH_VAR} is not a directory: {dump_path}")
    settings[DUMP_PATH_VAR] = dump_path

    return settings


def build_client(settings):
    return boto3.client(
        "s3",
        aws_access_key_id=settings["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=settings["S3_SECRET_ACCESS_KEY"],
        endpoint_url=settings["S3_ENDPOINT_URL"],
        region_name=settings["S3_REGION"],
        config=Config(signature_version="s3v4"),
    )


def collect_dumps(dump_path):
    names = sorted(
        name
        for name in os.listdir(dump_path)
        if os.path.isfile(os.path.join(dump_path, name))
    )
    return [(name, os.path.join(dump_path, name)) for name in names]


def upload_dumps(settings):
    files = collect_dumps(settings[DUMP_PATH_VAR])
    if not files:
        print(f"Nothing to upload: {settings[DUMP_PATH_VAR]} has no files.")
        return 0

    client = build_client(settings)
    bucket = settings["S3_BUCKET"]
    prefix = settings["S3_PREFIX"]

    target = f"{bucket}/{prefix}" if prefix else bucket
    print(f"Uploading {len(files)} file(s) from {settings[DUMP_PATH_VAR]} to {target}...")

    failures = 0
    for name, path in files:
        key = f"{prefix}/{name}" if prefix else name
        size_mb = os.path.getsize(path) / (1024 * 1024)
        try:
            client.upload_file(path, bucket, key)
            print(f"  uploaded {name} ({size_mb:.1f} MB) -> s3://{bucket}/{key}")
        except (BotoCoreError, ClientError) as error:
            failures += 1
            print(f"  FAILED {name}: {error}", file=sys.stderr)

    if failures:
        print(f"Done with {failures} failure(s) out of {len(files)}.", file=sys.stderr)
    else:
        print(f"Success! {len(files)} file(s) uploaded.")

    return failures


if __name__ == "__main__":
    try:
        settings = load_settings()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        sys.exit(1)

    if upload_dumps(settings):
        sys.exit(1)
