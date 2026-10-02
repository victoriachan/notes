# notes.madebyvictoria.uk

Self-hosted, gist-like markdown notes. Paste markdown, get a clean shareable
URL, optionally protect a note with a password.

Live at https://notes.madebyvictoria.uk/. This is a fork of Tom Dyson's
[notes.tomd.org](https://github.com/tomdyson/notes.tomd.org) (the `upstream`
remote); pull his changes with `git pull upstream main`. Fork-specific
settings live in env vars, so many code defaults still say `notes.tomd.org`.

## Features

- Single-user authoring (Django superuser), public/anonymous viewing
- Custom slugs or auto-generated 6-char base62 IDs (`notes.madebyvictoria.uk/aB3kLm`)
- Optional per-note passwords, session-scoped unlock, with IP+slug rate limiting
- Live markdown preview in the editor (client-side `marked` + `DOMPurify` +
  Mermaid + Highlight.js); server-side `markdown` + `pygments` + `bleach` is
  canonical
- Mermaid diagrams from fenced `mermaid` code blocks
- Drag or paste images into the editor — Pillow re-encodes to WebP, caps the
  longest edge at 2000px, strips EXIF, and stores on the persistent volume
- Rendered images are wrapped in click-to-expand links
- Raw source view at `/<slug>/raw`
- Passkey (WebAuthn) auth alongside username/password, with the RP ID set per
  deployment by `WEBAUTHN_RP_ID`
- Deployed on Railway with SQLite on a persistent volume, backed up daily to
  Cloudflare R2

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
DEBUG=1 python manage.py migrate
DEBUG=1 python manage.py createsuperuser
DEBUG=1 python manage.py runserver
```

## Tests

```bash
python manage.py test notes
```

The test suite covers slug generation, markdown rendering + XSS sanitisation,
Mermaid fence detection (including CRLF-submitted textareas), the `Note`
model, public read views, URL-shadowing guards, auth-gated authoring,
password gating, rate limiting, editor markup, UI structure, the `Passkey`
model, both WebAuthn flows (register + login, with crypto verification
mocked), the `Image` model + cascade/signal cleanup, the upload endpoint
(auth + CSRF), the Pillow pipeline (resize, WebP, EXIF-stripping), upload
rejection (SVG / oversized / non-image), image→expand-link wrapping, the
orphan-image sweep command, bearer-token note creation, reading and updating,
idempotent API retries, the bundled note-sharing skill clients, and the R2
backup command (snapshot contents, upload, pruning).

## URL map

| Path | Who | Purpose |
|---|---|---|
| `/` | public | Dashboard when logged in; a minimal page with a "Log in" link otherwise |
| `/login/` | anon | Django login; also exposes passkey login |
| `/new/` | authed | Editor for a new note |
| `/api/v1/notes` | bearer token | POST JSON to create and share a note |
| `/api/v1/notes/<slug>` | bearer token | GET a note's Markdown and HTML; PATCH to change it |
| `/api/v1/notes/<slug>/comments[/<id>]` | bearer token | List, post and delete comments |
| `/upload/` | authed | POST-only image upload (multipart), returns JSON `{url, markdown}` |
| `/i/<short_id>.webp` | public | Serve a stored image |
| `/<slug>/` | public | Rendered note (password-gated if set) |
| `/<slug>/raw` | public | Markdown source (password-gated if set) |
| `/<slug>/edit/` | authed | Editor for existing note |
| `/<slug>/unlock/` | public | Password prompt |
| `/<slug>/delete/` | authed | POST-only delete |
| `/passkeys/` | authed | List + register passkeys |
| `/passkeys/register/{begin,finish}/` | authed | WebAuthn register ceremony |
| `/passkeys/login/{begin,finish}/` | anon | WebAuthn auth ceremony |
| `/passkeys/<pk>/delete/` | authed | POST-only delete a passkey |
| `/admin/` | authed | Django admin |

Slugs `admin`, `login`, `logout`, `new`, `static`, `favicon.ico`, `robots.txt`,
`healthz`, `_`, `api`, `i`, `upload` are reserved. `/passkeys/` is also
effectively reserved because the literal `/passkeys/` path is registered
before the `<slug>/` catch-all.

## Agent sharing API

`POST /api/v1/notes` creates a note using the same validation, Markdown
rendering, password hashing and slug generation as the browser editor. Tokens
are scoped, revocable, and stored as SHA-256 digests; the raw secret is shown
only when it is created.

Create a token in the environment whose database the API will use:

```sh
python manage.py create_note_api_token --username victoria --name Codex
```

In production, run that command inside the running Railway container using the
procedure under "Running a management command in prod" below. Store the
printed token in the agent's secret/environment configuration as
`NOTES_TOMD_TOKEN`; do not put it in this repository or in a skill file. Delete
the token in Django admin to revoke it.

Example request:

```sh
curl https://notes.madebyvictoria.uk/api/v1/notes \
  -H "Authorization: Bearer $NOTES_TOMD_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  --data '{"title":"Example","markdown":"# Hello"}'
```

Accepted JSON fields are `markdown` (required), `title`, `slug`, `password`,
and `comments_enabled` (boolean, default false). A successful response
includes `url`, `raw_url`, timestamps, and whether the note is
password-protected and open to comments. Notes without a password are
accessible to anyone who has or discovers their URL. Requests default to a
1 MiB limit; override it with `NOTE_API_MAX_REQUEST_BYTES` if needed.

### Reading and updating notes

| Request | Scope | Does |
| --- | --- | --- |
| `GET /api/v1/notes/<slug>` | `notes:read` | Returns the note's settings plus its `markdown` source and rendered `html` |
| `PATCH /api/v1/notes/<slug>` | `notes:write` | Changes only the fields sent; returns the note as `GET` does |

Like the comments API, these act as the owner, so they ignore the note's
password. `PATCH` accepts `markdown`, `title` (`null` clears it), `slug`,
`comments_enabled`, `password` (sets a new one) and `clear_password: true`
(removes it). It validates through the same `NoteForm` as the editor. A blank
`slug` is refused, because it would give the note a new random URL. It has no
`Idempotency-Key` support, since repeating the same change has the same result.
Notes keep no revision history, so a client that replaces the Markdown should
keep the previous source itself.

```sh
curl -X PATCH https://notes.madebyvictoria.uk/api/v1/notes/abc123 \
  -H "Authorization: Bearer $NOTES_TOMD_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"title":"Revised","comments_enabled":true}'
```

### Comments

| Request | Scope | Does |
| --- | --- | --- |
| `GET /api/v1/notes/<slug>/comments` | `comments:read` | Lists threads with nested `replies` and each thread's `anchor` |
| `POST /api/v1/notes/<slug>/comments` | `comments:write` | Posts a comment as the note's owner (`is_owner`, "author" badge) |
| `DELETE /api/v1/notes/<slug>/comments/<id>` | `comments:write` | Deletes a comment and its replies; returns 204 |

The token acts as the owner, so reads ignore the note's password and work even
when comments are switched off. Posting to a note with comments off returns
`409 comments_disabled`. `POST` accepts `body` (required), `parent` (id of a
top-level comment, for a reply), `author_name`, and the anchor fields `quote`,
`prefix`, `suffix`, `start_offset`; it honours `Idempotency-Key` like note
creation. A `quote` must be the words as rendered on the page, not Markdown
source. Anchors are resolved in the reader's browser, so the API only reports
`anchor.quote_in_note`: whether the quote currently appears verbatim in the
note's rendered text.

Tokens are created with `notes:create` only. Grant read, edit and comment
access when issuing a token, or to an existing one by its prefix (shown in
Django admin) without changing the secret:

```sh
python manage.py create_note_api_token --username victoria --name Agent \
  --scopes "notes:create notes:read notes:write comments:read comments:write"
python manage.py set_note_api_token_scopes --prefix nt_AbCdEf123 \
  --scopes "notes:create notes:read notes:write comments:read comments:write"
```

The version-controlled personal skill is in `skills/share-notes/` and is
symlinked to `~/.claude/skills/share-notes/` on this machine. It reads
`NOTES_TOMD_TOKEN` and `NOTES_TOMD_API_URL`
(`https://notes.madebyvictoria.uk/api/v1/notes`) from `~/.zprofile`, which its
wrapper scripts load. It triggers only
on explicit sharing/publication requests and invokes its deterministic Python
client, which uses the API's idempotency support for safe retries. Its
`note_content` script reads and updates notes, and its `note_comments` script
lists, posts, replies to and deletes comments, through the endpoints above.

## Deployment

Deployed on [Railway](https://railway.com) as a single service built from the
`Dockerfile`. Pushes to `main` are built and deployed automatically. There is
**no CI**, so nothing runs the test suite on push; run
`python manage.py test notes` locally first. (`fly.toml` is a leftover from
when the upstream site ran on Fly and is unused.)

### Railway configuration

- `railway.toml` sets the Dockerfile builder, a `/login/` healthcheck (`/`
  isn't used, to keep the check on a cheap page) and restart-on-failure
- A volume mounted at `/app/data`, SQLite DB at `/app/data/db.sqlite3`
- Uploaded images live alongside the DB at `/app/data/media/images/` —
  `MEDIA_ROOT` defaults to `dirname(DB_PATH)/media`, so setting `DB_PATH`
  pins the media dir onto the same volume automatically
- Migrations run inside the app container at startup via `entrypoint.sh`; keep
  them there (a build/release step that doesn't mount the volume would migrate
  an empty DB — see CLAUDE.md)
- `entrypoint.sh` binds gunicorn to `$PORT` (default 8000) and creates/updates
  the superuser idempotently when the `DJANGO_SUPERUSER_*` env vars are set
- Custom domain `notes.madebyvictoria.uk`: a Cloudflare CNAME to the target
  Railway gives, with the domain's target port set to **8000**. If the record
  is proxied (orange cloud), Cloudflare's SSL/TLS mode must be Full or Full
  (strict), never Flexible
- `healthcheck.railway.app` is always added to `ALLOWED_HOSTS` in settings so
  Railway's healthcheck isn't rejected

### Environment variables

Set on the service's Variables tab. Railway stages changes, so **apply/deploy
them** afterwards or they won't reach the container.

- `SECRET_KEY` — Django secret key
- `DB_PATH` — `/app/data/db.sqlite3`
- `PORT` — `8000`; must match the custom domain's target port, or the site
  times out
- `WEBAUTHN_RP_ID` — `notes.madebyvictoria.uk`. Passkeys are bound to this,
  so changing it invalidates every registered passkey
- `ALLOWED_HOSTS` — `notes.madebyvictoria.uk`
- `CSRF_TRUSTED_ORIGINS` — `https://notes.madebyvictoria.uk`
- `RAILWAY_RUN_UID` — `0`, so the container runs as root and SQLite can write
  to the root-owned volume
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
  `R2_BUCKET_NAME` (`notes-madebyvictoria`), optional `R2_BUCKET_PREFIX`
  (default `notes-backups`) — for backups, below
- Optional `DJANGO_SUPERUSER_USERNAME` / `DJANGO_SUPERUSER_EMAIL` /
  `DJANGO_SUPERUSER_PASSWORD`. While both username and password are set, every
  start resets that user's password to the variable, so remove the password
  once you've logged in and registered a passkey

### Running a management command in prod

With the Railway CLI logged in (`railway login`, in a normal terminal) and this
directory linked (`railway link`):

```sh
railway logs
railway ssh -- python manage.py <command>
```

Quote the whole remote command when an argument contains spaces, as the remote
shell re-splits it:

```sh
railway ssh 'python manage.py create_note_api_token --username victoria --scopes "notes:create notes:read"'
```

e.g. `rerender_notes` re-renders every note's stored HTML after a rendering
change (`--dry-run` to preview the count).

Optional image-tuning overrides (defaults fine for most cases):
`MEDIA_ROOT`, `IMAGE_MAX_UPLOAD_BYTES` (default 10 MB),
`IMAGE_MAX_DIMENSION` (default 2000 px), `IMAGE_WEBP_QUALITY` (default 85).
The agent note API also accepts `NOTE_API_MAX_REQUEST_BYTES` (default 1 MiB).

### Backups

Railway's own volume backups need the Pro plan, so the database and images are
backed up to Cloudflare R2 instead:

```sh
railway ssh -- python manage.py backup_to_r2            # keeps the newest 10
railway ssh -- python manage.py backup_to_r2 --keep 30  # keep more
railway ssh -- python manage.py backup_to_r2 --keep 0   # never delete
```

The command:

1. snapshots the database with SQLite's online backup API, which is safe while
   the app is serving;
2. packs it with `media/` into `notes-<UTC timestamp>.tar.gz`;
3. uploads it to `<R2_BUCKET_PREFIX>/` in `R2_BUCKET_NAME`;
4. only then deletes the oldest `notes-*.tar.gz` under that prefix beyond
   `--keep`. Pruning is by count, not age, so if backups stop running the last
   ten are never expired out from under you. Other files in the bucket are
   left alone.

The R2 API token needs Object Read & Write on the bucket (upload, list and
delete).

**Schedule:** a launchd job on Victoria's Mac runs the command daily at 03:30
(`~/Library/LaunchAgents/uk.madebyvictoria.notes-backup.plist`, logging to
`~/Library/Logs/notes-backup.log`). launchd runs a missed job after the Mac
wakes, but skips days it was off. It depends on the Railway CLI staying logged
in; if backups stop, look for "Unauthorized" in the log and `railway login`
again. To run one now:

```sh
launchctl kickstart gui/$(id -u)/uk.madebyvictoria.notes-backup
```

**Restore:** download an archive from the bucket, extract it
(`tar -xzf notes-<stamp>.tar.gz` gives `db.sqlite3` and `media/`), and put both
back under `/app/data` on the volume, or point a local `DB_PATH` at the
extracted file to inspect it.

### TODO: schedule the orphan-image sweep

Uploads that never get referenced by a saved note are left with `note IS NULL`
and swept by a management command:

```
python manage.py sweep_orphan_images           # deletes orphans older than 24h
python manage.py sweep_orphan_images --hours 1 # shorter threshold
python manage.py sweep_orphan_images --dry-run # report only
```

Currently this has to be run manually (`railway ssh -- python manage.py
sweep_orphan_images`). It could join the daily launchd job so the volume
doesn't slowly accumulate dead uploads. A note-delete already cascades its images, so the sweep only handles
the "uploaded but never saved" case.

## Project layout

```
noteserver/       Django project (settings, root URLs, wsgi)
notes/            App
  models.py         Note + Image + Passkey + API token/idempotency models
  rendering.py      markdown → sanitised HTML (wraps images in expand-links)
  slugs.py          generate_slug(), reserved set, shape validation
  gate.py           password-unlock session + rate limiter
  forms.py          NoteForm, UnlockForm
  views.py          note CRUD, public read, agent API, image upload/serve
  images.py         Pillow pipeline: validate, resize, WebP re-encode
  passkey_views.py  WebAuthn register / login / manage
  management/commands/  maintenance commands, API token creation, R2 backups
  static/notes/     editor.js, passkeys.js, site.css, pygments.css
  templates/notes/  base.html + page templates
  tests/            Unit + integration tests
skills/
  share-notes/      Personal agent skill + deterministic API client
Dockerfile        Python 3.13 slim; collectstatic at build with manifest storage
entrypoint.sh     migrate + superuser sync + gunicorn on $PORT
railway.toml      Railway build, healthcheck and restart policy
fly.toml          legacy upstream Fly config — unused
```

## Security notes

- Bleach allowlists safe tags/attrs after markdown rendering; `<script>` /
  `<style>` bodies are stripped pre-bleach. `javascript:` and other non-safe
  protocols are filtered.
- Links get `rel="nofollow noopener"` via a bleach linker callback.
- Password hashes use Django's `make_password`/`check_password`; raw values
  never stored.
- Note API bearer tokens are high-entropy, stored only as SHA-256 digests,
  scoped (`notes:create`, `notes:read`, `notes:write`, `comments:read`,
  `comments:write`; note creation only by default), revocable, and never
  accepted from query strings.
- Unlock throttle: 3 wrong attempts per `(IP, slug, minute)` → 429.
- WebAuthn RP ID comes from `WEBAUTHN_RP_ID` (`notes.madebyvictoria.uk` in
  prod) — passkeys registered in prod will not work against any other
  hostname, including the `*.up.railway.app` domain. Moving hosts is fine as
  long as the domain stays the same.
- Image uploads are validated by Pillow's decoder (not by `Content-Type` or
  filename), re-encoded to WebP, and size-capped; SVG is explicitly rejected
  because bleach does not sanitise image bodies. Re-encoding strips EXIF.
