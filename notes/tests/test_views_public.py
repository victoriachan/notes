from django.test import TestCase
from django.urls import reverse

from notes.models import Note


class HomeTests(TestCase):
    def test_home_anonymous_renders_public_page(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "<header")
        self.assertContains(r, 'href="/login/"')
        self.assertContains(r, "Private notes live here.")


class ViewNoteTests(TestCase):
    def test_view_note_renders_cached_html(self):
        n = Note.objects.create(slug="hello", markdown="# Hi\n\nbody")
        r = self.client.get("/hello/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Hi")
        self.assertContains(r, "body")

    def test_view_note_renders_title_in_head(self):
        Note.objects.create(slug="hello", title="My Note", markdown="x")
        r = self.client.get("/hello/")
        self.assertContains(r, "<title>My Note")

    def test_view_note_404_for_missing_slug(self):
        r = self.client.get("/nope/")
        self.assertEqual(r.status_code, 404)


class RawNoteTests(TestCase):
    def test_raw_returns_markdown_as_text_plain(self):
        Note.objects.create(slug="hello", markdown="# raw source\n")
        r = self.client.get("/hello/raw")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/plain", r["Content-Type"])
        self.assertEqual(r.content.decode("utf-8"), "# raw source\n")

    def test_raw_404_for_missing_slug(self):
        r = self.client.get("/nope/raw")
        self.assertEqual(r.status_code, 404)


class UrlShadowingTests(TestCase):
    def test_login_not_shadowed_by_note_route(self):
        # GET /login/ must resolve to the login view even if a note has that slug.
        # We bypass form validation by writing directly to the DB.
        Note.objects.create(slug="login", markdown="x")
        r = self.client.get("/login/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "name=\"username\"")

    def test_admin_not_shadowed_by_note_route(self):
        Note.objects.create(slug="admin", markdown="x")
        r = self.client.get("/admin/", follow=False)
        # Django admin redirects anonymous to login
        self.assertIn(r.status_code, (200, 302))
        if r.status_code == 302:
            self.assertIn("login", r["Location"])

    def test_new_not_shadowed_by_note_route(self):
        Note.objects.create(slug="new", markdown="x")
        r = self.client.get("/new/", follow=False)
        # /new/ requires login; expect redirect to /login/, not the note content.
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])


class NoteContentsTests(TestCase):
    def test_note_with_two_headings_links_each_section(self):
        Note.objects.create(slug="toc", markdown="## Alpha\n\ntext\n\n## Beta *b*\n\nmore")
        r = self.client.get("/toc/")
        self.assertContains(r, "data-note-toc")
        self.assertContains(r, 'href="#alpha"')
        self.assertContains(r, 'href="#beta-b"')
        self.assertContains(r, ">Beta b</a>")

    def test_note_with_one_heading_has_no_contents(self):
        Note.objects.create(slug="one", markdown="## Only\n\ntext")
        r = self.client.get("/one/")
        self.assertNotContains(r, "data-note-toc")

    def test_contents_sit_in_the_side_column_without_comments(self):
        Note.objects.create(slug="toc", markdown="## Alpha\n\n## Beta", comments_enabled=False)
        r = self.client.get("/toc/")
        self.assertContains(r, "note-toc-layout")
        self.assertContains(r, "max-w-7xl")

    def test_contents_sit_above_the_comment_rail(self):
        Note.objects.create(slug="toc", markdown="## Alpha\n\n## Beta", comments_enabled=True)
        body = self.client.get("/toc/").content.decode()
        self.assertIn("note-toc-layout", body)
        self.assertLess(body.index("data-note-toc"), body.index('id="comments"'))

    def test_note_without_contents_keeps_its_layout(self):
        Note.objects.create(slug="plain", markdown="text", comments_enabled=False)
        r = self.client.get("/plain/")
        self.assertNotContains(r, "note-toc-layout")
        self.assertNotContains(r, "max-w-7xl")


class ReservedIdViewTests(TestCase):
    def test_comments_heading_leaves_one_comments_id_on_the_page(self):
        Note.objects.create(slug="clash", markdown="## Comments\n\n## Other", comments_enabled=True)
        body = self.client.get("/clash/").content.decode()
        self.assertEqual(body.count('id="comments"'), 1)
        self.assertIn('href="#note-comments"', body)
