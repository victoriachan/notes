#!/usr/bin/env python3
"""Read and update the content of notes.tomd.org notes through the API."""

import argparse
import json
import os
import sys
from pathlib import Path

# Reuse the publishing client's HTTP, retry and error handling.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from share_note import (  # noqa: E402
    DEFAULT_API_URL,
    ShareNoteError,
    api_request,
    read_input,
    slug_from,
)


def note_url(api_url, slug):
    return f"{api_url.rstrip('/')}/{slug}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Read or update a notes.tomd.org note. Prints JSON."
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("note", help="Note slug or URL.")
    common.add_argument("--api-url", help="Override NOTES_TOMD_API_URL for local testing.")
    common.add_argument("--timeout", type=float, default=30.0)
    commands = parser.add_subparsers(dest="command", required=True)

    get = commands.add_parser("get", parents=[common], help="Print the note, including its Markdown.")
    get.add_argument(
        "--markdown", action="store_true", help="Print only the Markdown source, not JSON."
    )

    update = commands.add_parser(
        "update", parents=[common], help="Change the note. Only the parts given are changed."
    )
    update.add_argument(
        "source", nargs="?",
        help="File holding the new Markdown, or - to read stdin. Omit to keep the text.",
    )
    update.add_argument("--title", help="New title; an empty string removes it.")
    update.add_argument("--slug", help="New slug. The old URL stops working.")
    update.add_argument(
        "--comments", action=argparse.BooleanOptionalAction, default=None,
        help="Turn reader comments on or off.",
    )
    password = update.add_mutually_exclusive_group()
    password.add_argument(
        "--password-env", metavar="NAME",
        help="Set the note password from environment variable NAME.",
    )
    password.add_argument(
        "--clear-password", action="store_true", help="Remove the note's password."
    )
    return parser.parse_args(argv)


def update_payload(args):
    payload = {}
    if args.source is not None:
        payload["markdown"] = read_input(args.source, "Markdown")
    if args.title is not None:
        payload["title"] = args.title
    if args.slug is not None:
        payload["slug"] = args.slug
    if args.comments is not None:
        payload["comments_enabled"] = args.comments
    if args.password_env:
        if args.password_env not in os.environ:
            raise ShareNoteError(
                f"Password environment variable {args.password_env} is not configured."
            )
        payload["password"] = os.environ[args.password_env]
    if args.clear_password:
        payload["clear_password"] = True
    if not payload:
        raise ShareNoteError(
            "Nothing to update: give new Markdown or a --title, --slug, --comments, "
            "--password-env or --clear-password option."
        )
    return payload


def main(argv=None):
    args = parse_args(argv)
    token = os.environ.get("NOTES_TOMD_TOKEN", "").strip()
    if not token:
        raise ShareNoteError("NOTES_TOMD_TOKEN is not configured.")
    url = note_url(
        args.api_url or os.environ.get("NOTES_TOMD_API_URL", DEFAULT_API_URL),
        slug_from(args.note),
    )

    if args.command == "get":
        result = api_request("GET", url, token=token, timeout=args.timeout)
        if args.markdown:
            markdown = result.get("markdown", "")
            sys.stdout.write(markdown if markdown.endswith("\n") else markdown + "\n")
            return
    else:
        result = api_request(
            "PATCH", url, token=token, payload=update_payload(args), timeout=args.timeout
        )

    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except ShareNoteError as exc:
        json.dump({"error": str(exc)}, sys.stderr)
        sys.stderr.write("\n")
        raise SystemExit(1)
