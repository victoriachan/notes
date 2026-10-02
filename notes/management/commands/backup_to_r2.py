import os
import re
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

BACKUP_NAME_RE = re.compile(r"^notes-\d{8}T\d{6}Z\.tar\.gz$")
REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")


class Command(BaseCommand):
    help = (
        "Snapshot the SQLite database and media into a .tar.gz and upload it "
        "to Cloudflare R2 (configured by the R2_* environment variables)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--keep",
            type=int,
            default=10,
            help="After uploading, delete all but the newest N backups (default: 10; 0 keeps all).",
        )

    def handle(self, *args, keep, **kwargs):
        env = {name: os.environ.get(name, "").strip() for name in REQUIRED_ENV}
        missing = [name for name, value in env.items() if not value]
        if missing:
            raise CommandError(f"Missing environment variables: {', '.join(missing)}")
        prefix = os.environ.get("R2_BUCKET_PREFIX", "notes-backups").strip().strip("/")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        name = f"notes-{stamp}.tar.gz"
        key = f"{prefix}/{name}" if prefix else name

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite3")
            self._snapshot_database(db_path)
            archive_path = os.path.join(tmp, name)
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(db_path, arcname="db.sqlite3")
                if os.path.isdir(settings.MEDIA_ROOT):
                    archive.add(settings.MEDIA_ROOT, arcname="media")

            client = boto3.client(
                "s3",
                endpoint_url=f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
            )
            client.upload_file(
                archive_path,
                env["R2_BUCKET_NAME"],
                key,
                ExtraArgs={"ContentType": "application/gzip"},
            )
        self.stdout.write(f"Uploaded s3://{env['R2_BUCKET_NAME']}/{key}")
        if keep > 0:
            self._prune(client, env["R2_BUCKET_NAME"], prefix, keep)

    def _prune(self, client, bucket, prefix, keep):
        # Runs only after a successful upload, so a broken schedule never
        # deletes the backups it has stopped replacing. Timestamped names
        # sort chronologically.
        list_prefix = f"{prefix}/" if prefix else ""
        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=list_prefix
        )
        keys = sorted(
            obj["Key"]
            for page in pages
            for obj in page.get("Contents", [])
            if BACKUP_NAME_RE.match(obj["Key"][len(list_prefix):])
        )
        stale = keys[:-keep]
        # delete_objects accepts at most 1000 keys per request.
        for start in range(0, len(stale), 1000):
            batch = stale[start:start + 1000]
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        if stale:
            self.stdout.write(f"Deleted {len(stale)} old backup(s), kept {keep}.")

    def _snapshot_database(self, path):
        # SQLite's online backup API gives a consistent copy while the app
        # keeps serving, unlike copying the file (which can catch a write).
        connection.ensure_connection()
        target = sqlite3.connect(path)
        try:
            connection.connection.backup(target)
        finally:
            target.close()
