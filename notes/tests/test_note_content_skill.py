import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest import TestCase, mock


SCRIPT_PATH = (
    Path(__file__).parents[2] / "skills" / "share-notes" / "scripts" / "note_content.py"
)
SPEC = importlib.util.spec_from_file_location("note_content_skill", SCRIPT_PATH)
note_content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(note_content)

TOKEN_ENV = {"NOTES_TOMD_TOKEN": "nt_secret"}
NOTE_URL = "https://notes.tomd.org/api/v1/notes/abc123"


class NoteContentMainTests(TestCase):
    def run_main(self, argv, *, result=None, stdin="", env=TOKEN_ENV):
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(sys, "stdin", io.StringIO(stdin)),
            mock.patch.object(sys, "stdout", stdout),
            mock.patch.object(
                note_content, "api_request", return_value=result or {"slug": "abc123"}
            ) as api_request,
        ):
            note_content.main(argv)
        return api_request, stdout.getvalue()

    def payload(self, argv, **kwargs):
        api_request, _ = self.run_main(argv, **kwargs)
        self.assertEqual(api_request.call_args.args[:2], ("PATCH", NOTE_URL))
        return api_request.call_args.kwargs["payload"]

    def test_get_fetches_the_note_by_url_and_prints_it(self):
        api_request, out = self.run_main(
            ["get", "https://notes.tomd.org/abc123/"],
            result={"slug": "abc123", "markdown": "# Hi"},
        )
        self.assertEqual(api_request.call_args.args[:2], ("GET", NOTE_URL))
        self.assertEqual(api_request.call_args.kwargs["token"], "nt_secret")
        self.assertNotIn("payload", api_request.call_args.kwargs)
        self.assertEqual(json.loads(out), {"slug": "abc123", "markdown": "# Hi"})

    def test_get_markdown_prints_only_the_source(self):
        _, out = self.run_main(
            ["get", "abc123", "--markdown"], result={"slug": "abc123", "markdown": "# Hi"}
        )
        self.assertEqual(out, "# Hi\n")

    def test_update_sends_markdown_from_a_file(self):
        with NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8") as source:
            source.write("# Revised\n")
            source.flush()
            payload = self.payload(["update", "abc123", source.name])
        self.assertEqual(payload, {"markdown": "# Revised\n"})

    def test_update_reads_markdown_from_stdin(self):
        self.assertEqual(
            self.payload(["update", "abc123", "-"], stdin="# From stdin"),
            {"markdown": "# From stdin"},
        )

    def test_update_prints_the_updated_note(self):
        _, out = self.run_main(
            ["update", "abc123", "--title", "New"], result={"slug": "abc123", "title": "New"}
        )
        self.assertEqual(json.loads(out), {"slug": "abc123", "title": "New"})

    def test_update_sends_only_the_settings_given(self):
        self.assertEqual(
            self.payload(["update", "abc123", "--title", "New", "--no-comments"]),
            {"title": "New", "comments_enabled": False},
        )
        self.assertEqual(
            self.payload(["update", "abc123", "--comments", "--slug", "renamed"]),
            {"comments_enabled": True, "slug": "renamed"},
        )

    def test_update_can_clear_the_title(self):
        self.assertEqual(self.payload(["update", "abc123", "--title", ""]), {"title": ""})

    def test_update_reads_password_from_named_environment_variable(self):
        payload = self.payload(
            ["update", "abc123", "--password-env", "NOTE_PASSWORD"],
            env={**TOKEN_ENV, "NOTE_PASSWORD": "note-secret"},
        )
        self.assertEqual(payload, {"password": "note-secret"})

    def test_update_refuses_a_missing_password_variable(self):
        with self.assertRaisesRegex(note_content.ShareNoteError, "NOTE_PASSWORD"):
            self.run_main(["update", "abc123", "--password-env", "NOTE_PASSWORD"])

    def test_update_can_clear_the_password(self):
        self.assertEqual(
            self.payload(["update", "abc123", "--clear-password"]), {"clear_password": True}
        )

    def test_password_and_clear_password_are_exclusive(self):
        with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
            self.run_main(
                ["update", "abc123", "--password-env", "P", "--clear-password"],
                env={**TOKEN_ENV, "P": "x"},
            )

    def test_update_with_nothing_to_change_is_refused_before_any_request(self):
        with self.assertRaisesRegex(note_content.ShareNoteError, "Nothing to update"):
            self.run_main(["update", "abc123"])

    def test_empty_markdown_is_refused(self):
        with self.assertRaisesRegex(note_content.ShareNoteError, "empty"):
            self.run_main(["update", "abc123", "-"], stdin="  \n")

    def test_api_url_override_from_environment(self):
        api_request, _ = self.run_main(
            ["get", "abc123"],
            env={**TOKEN_ENV, "NOTES_TOMD_API_URL": "http://localhost:8765/api/v1/notes/"},
        )
        self.assertEqual(api_request.call_args.args[1], "http://localhost:8765/api/v1/notes/abc123")

    def test_requires_token_without_echoing_one(self):
        with self.assertRaisesRegex(note_content.ShareNoteError, "NOTES_TOMD_TOKEN"):
            self.run_main(["get", "abc123"], env={})

    def test_rejects_things_that_are_not_a_note(self):
        with self.assertRaises(note_content.ShareNoteError):
            self.run_main(["get", "../etc"])
