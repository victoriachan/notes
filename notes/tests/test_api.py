import hashlib
import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings

from notes.models import Note, NoteApiIdempotencyRecord, NoteApiToken


User = get_user_model()


class NoteApiTokenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tom", password="pw")

    def test_issue_returns_secret_but_only_stores_digest(self):
        token, secret = NoteApiToken.issue(user=self.user, name="Codex")

        self.assertTrue(secret.startswith("nt_"))
        self.assertNotEqual(token.token_digest, secret)
        self.assertNotIn(secret, token.token_digest)
        self.assertEqual(token.prefix, secret[:12])
        self.assertTrue(token.permits("notes:create"))

    def test_create_token_command_prints_secret_once(self):
        out = StringIO()

        call_command("create_note_api_token", username="tom", name="Codex", stdout=out)

        output = out.getvalue()
        self.assertIn("nt_", output)
        self.assertIn("shown only once", output)
        self.assertEqual(NoteApiToken.objects.count(), 1)


class CreateNoteApiTests(TestCase):
    endpoint = "/api/v1/notes"

    def setUp(self):
        self.user = User.objects.create_user(username="tom", password="pw")
        self.token, self.secret = NoteApiToken.issue(user=self.user, name="Codex")

    def auth(self, secret=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {secret or self.secret}"}

    def post(self, payload, **extra):
        return self.client.post(
            self.endpoint,
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth(),
            **extra,
        )

    def test_requires_bearer_token(self):
        response = self.client.post(
            self.endpoint,
            data=json.dumps({"markdown": "hello"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")
        self.assertEqual(response["WWW-Authenticate"], "Bearer")
        self.assertFalse(Note.objects.exists())

    def test_bearer_auth_does_not_require_csrf_cookie(self):
        client = Client(enforce_csrf_checks=True)

        response = client.post(
            self.endpoint,
            data=json.dumps({"markdown": "hello"}),
            content_type="application/json",
            **self.auth(),
        )

        self.assertEqual(response.status_code, 201)

    def test_rejects_invalid_and_revoked_tokens(self):
        invalid = self.client.post(
            self.endpoint,
            data=json.dumps({"markdown": "hello"}),
            content_type="application/json",
            **self.auth("nt_not-a-real-token"),
        )
        self.assertEqual(invalid.status_code, 401)

        self.token.revoke()
        revoked = self.post({"markdown": "hello"})
        self.assertEqual(revoked.status_code, 401)
        self.assertFalse(Note.objects.exists())

    def test_requires_create_scope_and_active_user(self):
        self.token.scopes = "notes:read"
        self.token.save(update_fields=["scopes"])

        forbidden = self.post({"markdown": "hello"})
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["error"]["code"], "insufficient_scope")

        self.token.scopes = "notes:create"
        self.token.save(update_fields=["scopes"])
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        inactive = self.post({"markdown": "hello"})
        self.assertEqual(inactive.status_code, 401)

    def test_creates_note_and_returns_share_urls(self):
        response = self.post(
            {
                "title": "Agent note",
                "markdown": "# Hello\n\nShared by an agent.",
                "slug": "agent-note",
                "password": "secret",
            }
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["slug"], "agent-note")
        self.assertEqual(body["title"], "Agent note")
        self.assertEqual(body["url"], "http://testserver/agent-note/")
        self.assertEqual(body["raw_url"], "http://testserver/agent-note/raw")
        self.assertTrue(body["password_protected"])

        note = Note.objects.get(slug="agent-note")
        self.assertIn(">Hello</h1>", note.html)
        self.assertTrue(note.check_password("secret"))
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)

    def test_generates_slug_when_omitted(self):
        response = self.post({"markdown": "hello"})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.json()["slug"]), 6)

    def test_reports_json_and_form_validation_errors(self):
        malformed = self.client.post(
            self.endpoint,
            data="{",
            content_type="application/json",
            **self.auth(),
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(malformed.json()["error"]["code"], "invalid_json")

        invalid = self.post({"markdown": "hello", "slug": "admin"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "validation_error")
        self.assertIn("slug", invalid.json()["error"]["fields"])

        unknown = self.post({"markdown": "hello", "surprise": True})
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"]["code"], "unknown_fields")

    def test_rejects_non_json_and_oversized_password(self):
        non_json = self.client.post(
            self.endpoint,
            data="markdown=hello",
            content_type="application/x-www-form-urlencoded",
            **self.auth(),
        )
        self.assertEqual(non_json.status_code, 415)

        long_password = self.post({"markdown": "hello", "password": "x" * 129})
        self.assertEqual(long_password.status_code, 422)
        self.assertFalse(Note.objects.exists())

    def test_idempotency_key_replays_same_response(self):
        headers = {"HTTP_IDEMPOTENCY_KEY": "share-request-1"}
        first = self.post({"markdown": "hello"}, **headers)
        second = self.post({"markdown": "hello"}, **headers)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(Note.objects.count(), 1)
        self.assertEqual(second["Idempotent-Replay"], "true")

    def test_idempotency_record_does_not_expose_or_plain_hash_password(self):
        payload = {"markdown": "hello", "password": "guessable"}
        self.post(payload, HTTP_IDEMPOTENCY_KEY="protected-request")

        record = NoteApiIdempotencyRecord.objects.get()
        plain_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertNotEqual(record.request_digest, plain_digest)
        self.assertNotIn("password", record.response_body)
        self.assertNotIn("guessable", json.dumps(record.response_body))

    def test_idempotency_key_cannot_be_reused_for_different_input(self):
        headers = {"HTTP_IDEMPOTENCY_KEY": "share-request-1"}
        self.post({"markdown": "first"}, **headers)

        response = self.post({"markdown": "second"}, **headers)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "idempotency_conflict")
        self.assertEqual(Note.objects.count(), 1)

    def test_idempotency_key_has_length_limit(self):
        response = self.post(
            {"markdown": "hello"}, HTTP_IDEMPOTENCY_KEY="x" * 201
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Note.objects.exists())

    @override_settings(NOTE_API_MAX_REQUEST_BYTES=32)
    def test_rejects_request_bodies_over_configured_limit(self):
        response = self.post({"markdown": "x" * 100})

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")
        self.assertFalse(Note.objects.exists())


class NoteApiBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tom", password="pw")
        self.token, self.secret = NoteApiToken.issue(
            user=self.user, name="Agent", scopes="notes:read notes:write"
        )
        self.note = Note.objects.create(
            slug="plan", title="Plan", markdown="# Plan\n\nShip **it**."
        )
        self.endpoint = "/api/v1/notes/plan"

    def auth(self, secret=None):
        return {"HTTP_AUTHORIZATION": f"Bearer {secret or self.secret}"}

    def get(self, endpoint=None):
        return self.client.get(endpoint or self.endpoint, **self.auth())

    def patch(self, payload, endpoint=None, **extra):
        return self.client.patch(
            endpoint or self.endpoint,
            data=json.dumps(payload),
            content_type="application/json",
            **self.auth(),
            **extra,
        )

    def set_scopes(self, scopes):
        self.token.scopes = scopes
        self.token.save(update_fields=["scopes"])


class NoteScopeTests(TestCase):
    def test_read_and_write_are_known_scopes(self):
        self.assertEqual(
            NoteApiToken.normalize_scopes("notes:read notes:write"),
            "notes:read notes:write",
        )

    def test_tokens_still_default_to_creation_only(self):
        user = User.objects.create_user(username="tom", password="pw")
        token, _ = NoteApiToken.issue(user=user, name="Agent")
        self.assertFalse(token.permits("notes:read"))
        self.assertFalse(token.permits("notes:write"))


class ReadNoteApiTests(NoteApiBase):
    def test_requires_bearer_token(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")

    def test_requires_read_scope(self):
        self.set_scopes("notes:create notes:write comments:read")
        response = self.get()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "insufficient_scope")
        self.assertIn("notes:read", response.json()["error"]["message"])

    def test_unknown_note_is_a_json_404(self):
        response = self.get("/api/v1/notes/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_returns_markdown_and_rendered_html_with_metadata(self):
        response = self.get()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["slug"], "plan")
        self.assertEqual(body["title"], "Plan")
        self.assertEqual(body["markdown"], "# Plan\n\nShip **it**.")
        self.assertEqual(body["html"], self.note.html)
        self.assertIn("<strong>it</strong>", body["html"])
        self.assertEqual(body["url"], "http://testserver/plan/")
        self.assertFalse(body["password_protected"])
        self.assertFalse(body["comments_enabled"])
        self.assertEqual(body["updated_at"], self.note.updated_at.isoformat())

    def test_owner_token_reads_password_protected_note_without_the_hash(self):
        self.note.set_password("secret")
        self.note.save()

        response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["password_protected"])
        self.assertIn("Ship", response.json()["markdown"])
        self.assertNotIn(self.note.password_hash, response.content.decode())
        self.assertNotIn("password_hash", response.json())

    def test_only_get_and_patch_are_allowed(self):
        for method in (self.client.post, self.client.put, self.client.delete):
            response = method(
                self.endpoint, data="{}", content_type="application/json", **self.auth()
            )
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response["Allow"], "GET, PATCH")
        self.assertTrue(Note.objects.filter(slug="plan").exists())


class UpdateNoteApiTests(NoteApiBase):
    def assertUnchanged(self):
        self.note.refresh_from_db()
        self.assertEqual(self.note.slug, "plan")
        self.assertEqual(self.note.title, "Plan")
        self.assertEqual(self.note.markdown, "# Plan\n\nShip **it**.")

    def test_requires_write_scope(self):
        self.set_scopes("notes:create notes:read")
        response = self.patch({"markdown": "changed"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("notes:write", response.json()["error"]["message"])
        self.assertUnchanged()

    def test_bearer_auth_does_not_require_csrf_cookie(self):
        client = Client(enforce_csrf_checks=True)
        response = client.patch(
            self.endpoint,
            data=json.dumps({"title": "New"}),
            content_type="application/json",
            **self.auth(),
        )
        self.assertEqual(response.status_code, 200)

    def test_unknown_note_is_a_json_404(self):
        response = self.patch({"title": "x"}, endpoint="/api/v1/notes/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_updates_markdown_and_rerenders_leaving_other_fields_alone(self):
        self.note.set_password("secret")
        self.note.comments_enabled = True
        self.note.save()

        response = self.patch({"markdown": "# Plan\n\nShipped *today*."})

        self.assertEqual(response.status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.markdown, "# Plan\n\nShipped *today*.")
        self.assertIn("<em>today</em>", self.note.html)
        self.assertEqual((self.note.slug, self.note.title), ("plan", "Plan"))
        self.assertTrue(self.note.comments_enabled)
        self.assertTrue(self.note.check_password("secret"))
        body = response.json()
        self.assertEqual(body["markdown"], self.note.markdown)
        self.assertEqual(body["html"], self.note.html)
        self.assertTrue(body["password_protected"])

    def test_updates_title_alone(self):
        response = self.patch({"title": "Revised plan"})
        self.assertEqual(response.status_code, 200)
        self.note.refresh_from_db()
        self.assertEqual(self.note.title, "Revised plan")
        self.assertEqual(self.note.markdown, "# Plan\n\nShip **it**.")

    def test_null_title_clears_it(self):
        self.patch({"title": None})
        self.note.refresh_from_db()
        self.assertEqual(self.note.title, "")

    def test_toggles_comments(self):
        self.assertTrue(self.patch({"comments_enabled": True}).json()["comments_enabled"])
        self.note.refresh_from_db()
        self.assertTrue(self.note.comments_enabled)

    def test_renames_slug(self):
        response = self.patch({"slug": "launch-plan"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["url"], "http://testserver/launch-plan/")
        self.assertEqual(Note.objects.get().slug, "launch-plan")
        self.assertEqual(self.get().status_code, 404)

    def test_slug_cannot_be_emptied_reserved_or_taken(self):
        Note.objects.create(slug="taken", markdown="x")
        for slug in ("", "   ", None, "admin", "taken", "bad slug"):
            response = self.patch({"slug": slug})
            self.assertEqual(response.status_code, 422, slug)
            self.assertIn("slug", response.json()["error"]["fields"], slug)
        self.assertUnchanged()

    def test_sets_and_clears_password(self):
        self.assertTrue(self.patch({"password": "hunter2"}).json()["password_protected"])
        self.note.refresh_from_db()
        self.assertTrue(self.note.check_password("hunter2"))

        response = self.patch({"clear_password": True})
        self.assertFalse(response.json()["password_protected"])
        self.note.refresh_from_db()
        self.assertFalse(self.note.has_password)

    def test_response_never_echoes_the_password(self):
        response = self.patch({"password": "hunter2"})
        self.assertNotIn("hunter2", response.content.decode())

    def test_validation_errors(self):
        cases = [
            ({"markdown": ""}, "markdown"),
            ({"markdown": "   "}, "markdown"),
            ({"markdown": None}, "markdown"),
            ({"markdown": 5}, "markdown"),
            ({"title": 5}, "title"),
            ({"title": "t" * 201}, "title"),
            ({"comments_enabled": "yes"}, "comments_enabled"),
            ({"password": ""}, "password"),
            ({"password": None}, "password"),
            ({"password": "x" * 129}, "password"),
            ({"password": "new", "clear_password": True}, "password"),
            ({"clear_password": "yes"}, "clear_password"),
        ]
        for payload, field in cases:
            response = self.patch(payload)
            self.assertEqual(response.status_code, 422, payload)
            self.assertEqual(response.json()["error"]["code"], "validation_error")
            self.assertIn(field, response.json()["error"]["fields"], payload)
        self.assertUnchanged()
        self.assertFalse(self.note.has_password)

    def test_rejects_unknown_fields_bad_json_and_wrong_content_type(self):
        response = self.patch({"markdown": "x", "html": "<script>"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unknown_fields")
        self.assertEqual(response.json()["error"]["fields"], ["html"])

        response = self.client.patch(
            self.endpoint, data="{", content_type="application/json", **self.auth()
        )
        self.assertEqual(response.json()["error"]["code"], "invalid_json")

        response = self.client.patch(
            self.endpoint, data="markdown=x", content_type="text/plain", **self.auth()
        )
        self.assertEqual(response.status_code, 415)
        self.assertUnchanged()

    @override_settings(NOTE_API_MAX_REQUEST_BYTES=32)
    def test_rejects_request_bodies_over_configured_limit(self):
        response = self.patch({"markdown": "x" * 100})
        self.assertEqual(response.status_code, 413)
        self.assertUnchanged()
