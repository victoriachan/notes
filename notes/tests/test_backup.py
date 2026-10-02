import io
import os
import sqlite3
import tarfile
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TransactionTestCase, override_settings

from notes.models import Note

R2_ENV = {
    "R2_ACCOUNT_ID": "acct123",
    "R2_ACCESS_KEY_ID": "key",
    "R2_SECRET_ACCESS_KEY": "secret",
    "R2_BUCKET_NAME": "backups",
}


class BackupToR2Tests(TransactionTestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp(prefix="backup-media-")
        os.makedirs(os.path.join(self.media, "images"))
        with open(os.path.join(self.media, "images", "a.webp"), "wb") as f:
            f.write(b"webp-bytes")
        self.uploads = []

        def fake_upload(path, bucket, key, **kwargs):
            with open(path, "rb") as f:
                self.uploads.append((bucket, key, f.read()))

        self.client = mock.Mock()
        self.client.upload_file.side_effect = fake_upload
        self.existing_keys = []
        self.client.get_paginator.return_value.paginate.side_effect = lambda **kw: [
            {"Contents": [{"Key": k} for k in self.existing_keys + [u[1] for u in self.uploads]]}
        ]
        patcher = mock.patch(
            "notes.management.commands.backup_to_r2.boto3.client",
            return_value=self.client,
        )
        self.boto_client = patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, env=R2_ENV, *args):
        out = StringIO()
        with override_settings(MEDIA_ROOT=self.media), mock.patch.dict(
            os.environ, env, clear=False
        ):
            call_command("backup_to_r2", *args, stdout=out)
        return out.getvalue()

    def _archive(self):
        (_, _, data) = self.uploads[0]
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")

    def test_uploads_archive_to_bucket_under_prefix(self):
        out = self._run()
        self.assertEqual(len(self.uploads), 1)
        bucket, key, _ = self.uploads[0]
        self.assertEqual(bucket, "backups")
        self.assertRegex(key, r"^notes-backups/notes-\d{8}T\d{6}Z\.tar\.gz$")
        self.assertIn(key, out)
        kwargs = self.boto_client.call_args.kwargs
        self.assertEqual(kwargs["endpoint_url"], "https://acct123.r2.cloudflarestorage.com")
        self.assertEqual(kwargs["aws_access_key_id"], "key")

    def test_prefix_can_be_overridden(self):
        self._run({**R2_ENV, "R2_BUCKET_PREFIX": "/elsewhere/"})
        self.assertTrue(self.uploads[0][1].startswith("elsewhere/notes-"))

    def test_archive_holds_a_usable_database_snapshot(self):
        Note.objects.create(title="Keep me", markdown="hello")
        self._run()
        with tempfile.TemporaryDirectory() as tmp:
            self._archive().extract("db.sqlite3", tmp, filter="data")
            conn = sqlite3.connect(os.path.join(tmp, "db.sqlite3"))
            titles = [r[0] for r in conn.execute("select title from notes_note")]
            conn.close()
        self.assertEqual(titles, ["Keep me"])

    def test_archive_includes_media_files(self):
        self._run()
        member = self._archive().extractfile("media/images/a.webp")
        self.assertEqual(member.read(), b"webp-bytes")

    def test_missing_media_dir_is_not_an_error(self):
        os.rename(self.media, self.media + "-gone")
        self._run()
        names = self._archive().getnames()
        self.assertEqual(names, ["db.sqlite3"])

    def test_missing_credentials_fail_without_uploading(self):
        env = {k: v for k, v in R2_ENV.items() if k != "R2_SECRET_ACCESS_KEY"}
        with mock.patch.dict(os.environ, {"R2_SECRET_ACCESS_KEY": ""}):
            with self.assertRaisesMessage(CommandError, "R2_SECRET_ACCESS_KEY"):
                self._run(env)
        self.client.upload_file.assert_not_called()

    def _deleted_keys(self):
        return [
            obj["Key"]
            for call in self.client.delete_objects.call_args_list
            for obj in call.kwargs["Delete"]["Objects"]
        ]

    def _old_backups(self, count):
        # Keys sort oldest-first, and all predate the run's real timestamp.
        return [f"notes-backups/notes-20260101T{i:06d}Z.tar.gz" for i in range(count)]

    def test_keeps_only_the_newest_ten_backups_by_default(self):
        self.existing_keys = self._old_backups(10) + ["notes-backups/readme.txt"]
        self._run()
        # 10 old + the new upload = 11, so only the single oldest goes.
        self.assertEqual(self._deleted_keys(), self._old_backups(1))
        self.assertEqual(self.client.delete_objects.call_args.kwargs["Bucket"], "backups")

    def test_nothing_deleted_at_or_under_the_limit(self):
        self.existing_keys = self._old_backups(9)
        self._run()
        self.client.delete_objects.assert_not_called()

    def test_only_lists_under_the_prefix(self):
        self._run()
        self.client.get_paginator.return_value.paginate.assert_called_with(
            Bucket="backups", Prefix="notes-backups/"
        )

    def test_keep_option_sets_how_many_survive(self):
        self.existing_keys = self._old_backups(5)
        self._run(R2_ENV, "--keep", "3")
        self.assertEqual(self._deleted_keys(), self._old_backups(3))

    def test_keep_zero_disables_pruning(self):
        self.existing_keys = self._old_backups(20)
        self._run(R2_ENV, "--keep", "0")
        self.client.delete_objects.assert_not_called()

    def test_failed_upload_deletes_nothing(self):
        self.existing_keys = self._old_backups(20)
        self.client.upload_file.side_effect = RuntimeError("R2 down")
        with self.assertRaises(RuntimeError):
            self._run()
        self.client.delete_objects.assert_not_called()

    def test_no_temporary_files_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "tempfile.tempdir", tmp
        ):
            self._run()
            self.assertEqual(os.listdir(tmp), [])
