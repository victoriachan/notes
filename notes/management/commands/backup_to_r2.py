import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

import boto3
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")


class Command(BaseCommand):
    help = (
        "Snapshot the SQLite database and media into a .tar.gz and upload it "
        "to Cloudflare R2 (configured by the R2_* environment variables)."
    )

    def handle(self, *args, **kwargs):
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

    def _snapshot_database(self, path):
        # SQLite's online backup API gives a consistent copy while the app
        # keeps serving, unlike copying the file (which can catch a write).
        connection.ensure_connection()
        target = sqlite3.connect(path)
        try:
            connection.connection.backup(target)
        finally:
            target.close()
