# CLAUDE.md

Guidance for Claude working in this repo. Read before making changes.

## What this is

A single-user, self-hosted, gist-like Django app that serves markdown notes at
`notes.madebyvictoria.uk`. One Django project (`noteserver`), one app
(`notes`), SQLite on a persistent volume, deployed on Railway, which
auto-deploys on push to `main`.

This repo is Victoria's fork (`origin` = `victoriachan/notes`) of Tom Dyson's
`notes.tomd.org` (`upstream` = `tomdyson/notes.tomd.org`, which Tom deploys on
Coolify). Pull his changes with `git pull upstream main`; never push to
`upstream`. Keep fork-specific behaviour in env vars rather than code so
upstream merges stay clean — which is why many defaults, tests and the
`share-notes` skill still say `notes.tomd.org`. `fly.toml` is vestigial.

## Commands

- Python env: a local `.venv` (git-ignored). Use `.venv/bin/python`, or
  activate it; the system Python lacks the dependencies.
- Run tests: `.venv/bin/python manage.py test notes` (390 tests, ~30s; the
  anchoring JS tests need `node` on PATH and are skipped without it)
- Run a single test: `.venv/bin/python manage.py test notes.tests.test_rendering.RenderMarkdownTests.test_strips_script_tags`
- Dev server: `DEBUG=1 .venv/bin/python manage.py runserver`
- Migrations (local): `DEBUG=1 .venv/bin/python manage.py migrate`
- Deploy: push to `main` (Railway builds the `Dockerfile` and deploys).
- Prod logs / management commands: the `railway` CLI (see Deployment).

`DEBUG=1` is needed for any management command that touches settings outside
of `manage.py test` (tests auto-detect `test` in `argv`). Not needed in the
prod container — it already has production settings in its environment.

## Working style

- **Red/green TDD.** New behaviour starts with a failing test. Tests live in
  `notes/tests/test_*.py`, one file per concern: `test_slugs`, `test_rendering`,
  `test_models`, `test_views_public`, `test_views_auth`, `test_password_gate`,
  `test_editor_markup`, `test_ui_reorg`, `test_passkey_model`,
  `test_passkey_register`, `test_passkey_login`, `test_images_model`,
  `test_image_pipeline`, `test_image_rejection`, `test_image_gc`,
  `test_upload`, `test_comments_model`, `test_comments_views`,
  `test_comments_js` (runs `node --test notes/tests/js/anchors.test.mjs`),
  `test_api`, `test_api_comments`, `test_share_note_skill`,
  `test_note_comments_skill`, `test_note_content_skill`, `test_backup`. Django's built-in
  `TestCase` — not pytest.
- Don't add new abstractions without a test that motivates them.

## Architecture gotchas

- **URL ordering matters.** `noteserver/urls.py` registers `/admin/`,
  `/login/`, `/logout/` before including `notes.urls`; inside `notes/urls.py`
  the literal `/new/` is registered before the `<slug:slug>/` catch-all.
  Breaking this order lets a malicious (or accidental) note shadow those
  paths. There are explicit tests guarding this in `test_views_public.py`.
- **`Note.save()` renders HTML and auto-generates the slug.** Never set
  `html` manually; never bypass `save()` for slug generation. The slug
  generator retries on collision up to 8 times inside atomic savepoints.
- **Markdown rendering is canonical server-side.** The editor's live preview
  is `marked` + `DOMPurify` (client-side), but the stored `html` field is
  what readers see. Always sanitise through `notes/rendering.py`; don't add
  new tag/attr allowances without thinking about XSS.
  `render_markdown` prefixes any note id that matches the page's own
  (`comments`, `comment-<n>`) with `note-`, so a "Comments" heading can't
  capture the comment rail's htmx swaps. If the note page gains new ids
  around the body, add them to `_RESERVED_ID_RE`.
- **Mermaid fences are pre-processed before markdown.** `render_markdown`
  rewrites ```` ```mermaid ```` blocks into `<div class="mermaid">…</div>`
  *before* handing the source to `markdown` so pygments never sees them.
  The regex must tolerate CRLF (`\r?\n`) — browsers submit `<textarea>`
  contents with CRLF, so a LF-only match silently falls through to
  syntax-highlighted code and every diagram authored through the UI breaks.
  When adding tests, include at least one CRLF fixture.
- **Static files in prod use `CompressedManifestStaticFilesStorage`.** This
  requires `collectstatic` to run at Docker build time in non-DEBUG mode so
  the manifest exists. See `Dockerfile` — that's why the RUN line is
  `SECRET_KEY=build python manage.py collectstatic --noinput` (no DEBUG=1).
- **Anonymous `/` is a minimal public page.** `home()` renders
  `home_public.html` (just a "Notes" card) with `show_public_header`, so the
  header shows a single "Log in" link. Other anonymous pages render without a
  header; for authenticated users it contains New note / Passkeys / Log out,
  and `/` becomes the dashboard.
- **Passkey auth is built on `py_webauthn`, with RP ID fixed per deployment.**
  `WEBAUTHN_RP_ID` comes from the env var of the same name (default
  `notes.tomd.org`; prod sets `notes.madebyvictoria.uk`) — do not swap this
  for a request-derived value. A passkey is bound to the RP ID it was
  registered against, so changing it silently invalidates every existing
  passkey.
  `notes/passkey_views.py` holds the register/login ceremonies; state
  (challenge) lives in the session; `Passkey` rows belong to a user and
  store `credential_id`, `public_key`, `sign_count`.
- **Layout width: `container_class` template variable.** `base.html`'s header
  nav and main wrapper both render `{{ container_class|default:"max-w-4xl" }}`
  so they line up on every page. The editor also passes
  `container_class="max-w-4xl"` so its markdown pane lines up with the fixed
  footer below. Don't reintroduce per-template `max-w-*` wrappers inside
  content blocks — put the class on the view's context instead.
- **Editor settings live in the footer, outside `<form>`.** Title, slug and
  password render in `{% block after_main %}` — a slot defined in `base.html`
  that sits *outside* the constrained `<main>` so a fixed, full-width footer
  bar can span the viewport. The footer has a hidden `data-settings-panel`
  that expands upwards on toggle. Because those inputs live outside
  `<form id="editor-form">`, each one uses HTML5's `form="editor-form"`
  attribute to submit with the editor form. If you add a new `NoteForm`
  field, render it in the footer panel and include `form="editor-form"`,
  otherwise it will be silently dropped on submit.

- **Comments are gated with the note.** `notes/views.py` has `create_comment`
  and `delete_comment`; both call `_gate` *before* anything else, then 404
  when `note.comments_enabled` is off. Anonymous commenters are identified by
  `commenter_name` and `commenter_key` in the session (same session that
  holds the password unlock); a logged-in user's comments get `is_owner`.
  Rate limiting reuses `notes/gate.py` with `scope="comment"` so comment
  posts and unlock attempts on the same note count separately. Comment
  bodies are plain text rendered with `urlize|linebreaksbr` — never pass
  them through `render_markdown`. The anchor fields (`quote`, `prefix`,
  `suffix`, `start_offset`) are stored verbatim (`strip=False`) and exposed
  as `data-*` attributes on each thread's `<li>` for client-side anchoring.
- **Anchoring is resolved in the browser, never on the server.**
  `notes/static/notes/anchors.js` is a pure-string UMD module (exact at
  offset → exact anywhere, context picks between repeats → approximate match
  within 25% of the quote, which beyond 2 errors must also agree with the
  stored prefix/suffix → orphan). `comments.js` maps offsets over the
  concatenated text nodes of `.note-body`, skipping `.mermaid` (replaced by
  SVG at runtime), captures selections with the same mapping, wraps hits in
  `<mark class="comment-highlight">`, and reveals the `data-orphan-badge` on
  misses. Keep the algorithm in `anchors.js` so it stays testable under node.
  Don't put Tailwind display utilities (`flex`, `inline-block`) on elements
  toggled with the `hidden` attribute — the utility wins and the element
  shows.

- **The comments API acts as the owner, not as a visitor.**
  `api_note_comments` (GET/POST) and `api_note_comment` (DELETE) sit behind
  the `comments:read` / `comments:write` token scopes and deliberately skip
  `_gate`, the visitor rate limit and the session identity: API comments are
  saved with `is_owner=True`. POST still refuses when `comments_enabled` is
  off (409) and validates through the same `CommentForm`. New API endpoints
  should reuse the helpers in `views.py` (`_api_token_or_error`,
  `_json_object_or_error`, `_create_idempotently`) rather than re-implementing
  auth, size limits or idempotency. `quote_in_note` in responses is an
  advisory substring check on stripped HTML — it is not anchoring, which
  stays in the browser. New scopes go in `NoteApiToken.KNOWN_SCOPES`; tokens
  default to `notes:create` only. The `skills/share-notes/` scripts are the
  API's main client — keep `SKILL.md` and the README in step with API changes.

- **The note API reads and edits as the owner too.** `api_note` serves
  `GET`/`PATCH /api/v1/notes/<slug>` behind `notes:read` / `notes:write`, also
  without `_gate`. `PATCH` overlays the sent fields on the note's current
  values and saves through `NoteForm(instance=note)`, so validation, re-render
  and password hashing match the editor. It refuses a blank `slug`: `NoteForm`
  passes `""` through and `Note.save()` would then generate a fresh slug,
  silently moving the note to a new URL.

## SQLite on a volume — critical

Migrations run inside the app container at startup via `entrypoint.sh`
(`migrate --noinput`), **not** as a separate build/release step. The persistent
data volume is only mounted in the running app container — any migration step
that runs outside it (a build-time `RUN`, a Fly-style `release_command`, a
detached one-off without the volume) would execute against an empty ephemeral
file and silently succeed while doing nothing. Keep migrations in
`entrypoint.sh`.

The SQLite file lives on a Railway volume attached to the service at
`/app/data`; `DB_PATH` must point inside it (`/app/data/db.sqlite3`). Uploaded
images go to `/app/data/media` (derived from `DB_PATH`). Railway's volume
backups need the Pro plan, so backups go to Cloudflare R2 instead:
`python manage.py backup_to_r2` snapshots the database with SQLite's online
backup API (safe while the app runs), tars it with `media/`, and uploads
`<R2_BUCKET_PREFIX>/notes-<stamp>.tar.gz` (prefix defaults to
`notes-backups`). After a successful upload it deletes all but the newest
10 `notes-<stamp>.tar.gz` under the prefix (`--keep N`, `0` keeps all) — by
count, not age, so if the schedule stops the last backups survive. It needs `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY` and `R2_BUCKET_NAME` in the Railway variables. Run it
in prod with `railway ssh -- python manage.py backup_to_r2`; a launchd job on
Victoria's Mac (`~/Library/LaunchAgents/uk.madebyvictoria.notes-backup.plist`,
log `~/Library/Logs/notes-backup.log`) runs exactly that daily at 03:30. To restore,
extract the archive and put `db.sqlite3` and `media/` back under `/app/data`.

## Deployment

Deployed on Railway as a single service built from the `Dockerfile`
(`railway.toml` sets the builder, a `/login/` healthcheck and restart policy).
Custom domain `notes.madebyvictoria.uk` is a Cloudflare CNAME to Railway.

- **No CI.** Railway deploys on every push to `main` and nothing runs the
  tests, so run them locally *before* pushing.
- **Port:** `entrypoint.sh` binds gunicorn to `${PORT:-8000}`. The service sets
  `PORT=8000` explicitly and the custom domain's target port is 8000. Keep the
  two in step: if they differ, the site times out.
- **Env vars** (Railway service → Variables): `SECRET_KEY`, `WEBAUTHN_RP_ID`,
  `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DB_PATH`, `RAILWAY_RUN_UID=0` (runs
  the container as root so SQLite can write to the root-owned volume), and
  optionally `DJANGO_SUPERUSER_USERNAME`/`_PASSWORD` (when both are set,
  `entrypoint.sh` creates the superuser or resets its password on each start).
  `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` must include every domain that
  serves the site — with scheme for CSRF (`https://...`), without for
  ALLOWED_HOSTS. `healthcheck.railway.app` is always appended to
  `ALLOWED_HOSTS` in settings, for Railway's healthcheck.
- **Cloudflare:** if the record is proxied (orange cloud), SSL/TLS mode must
  be Full or Full (strict), never Flexible.
- **Prod logs / management commands:** `railway link` once in this directory,
  then `railway logs`, and `railway ssh -- python manage.py <command>` (e.g.
  `create_note_api_token`, `rerender_notes`). No `DEBUG=1` needed in the
  container.

## Things NOT to do

- Don't move migrations out of `entrypoint.sh` into a separate build/release
  step that doesn't mount the data volume (see SQLite note above).
- Don't add a deploy GitHub Action — Railway deploys on push. If you want CI
  to run tests on push, add a *test-only* workflow.
- Don't push to `upstream` (Tom's repo); every push to his `main` deploys
  `notes.tomd.org`.
- Don't switch `STORAGES` away from `CompressedManifestStaticFilesStorage` in
  prod — if you do, update `Dockerfile` and ensure whitenoise can still serve.
- Don't widen the bleach allowlist (`notes/rendering.py`) without a test that
  justifies it.
- Don't store passwords in plaintext, don't log them, don't serialise them.
  Use `Note.set_password` / `check_password`.
- Don't change the shape of `generate_slug()` (6-char base62) without
  considering URL collisions with already-published notes. If you shorten it,
  collisions get likelier; if you lengthen it, old URLs still work.
- Don't change prod's `WEBAUTHN_RP_ID` away from `notes.madebyvictoria.uk`,
  don't derive it from the request host, and don't add the `*.up.railway.app`
  domain as an alt origin — every existing passkey would stop working. Moving
  hosts (e.g. to Coolify) is fine as long as the domain stays the same.
