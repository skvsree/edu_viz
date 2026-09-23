#!/usr/bin/env python3
"""Download NCERT textbooks and push them into edu_viz through its bulk API.

Runs OUTSIDE the app: nothing here is imported by the running service. It talks
to edu_viz over HTTP using the same public endpoints the web UI uses, so the
existing ``bulk_ai_upload`` worker does the card generation, retries and progress
reporting exactly as it does for a manual upload.

What it does, per book:

    ensure folder  Class_XII              (POST /api/v1/folders)
      ensure folder  English              (POST /api/v1/folders, parent_id=class)
        download every chapter PDF
        zip them and upload ONCE         (POST /api/v1/bulk-ai-upload/start,
                                          folder_id=<subject folder>)
          -> the app creates one deck per chapter, named from its content

Why a zip per book, and not a deck per book: the bulk pipeline renames every
deck to the title it derives from the PDF's content, and the single-deck endpoint
(`/api/v1/decks/{id}/ai-import/start`) passes no folder so the worker clears the
deck's folder. Pre-creating a "book" deck therefore does not survive, and pushing
a second chapter into the same deck wipes the first chapter's cards
(`_clear_deck_generated_content`). One zip per book is the shape this API
actually supports, and it gives one bulk job per book instead of one per chapter.

Auth: the PDF bulk endpoints require the app session cookie, not the bulk-import
API key. Supply it with ``--cookie`` / ``--cookie-file``, or let the script mint
one from the app secret with ``--user-email`` (needs ``--secret-key`` and a
checkout of the app on ``PYTHONPATH``).

Examples
--------
    # see what would be imported
    ./ncert_bulk_import.py --base-url https://qa.edu.selviz.in --dry-run

    # import every English Class VI book
    ./ncert_bulk_import.py --base-url https://qa.edu.selviz.in \
        --cookie-file ~/.eduviz-session --class "Class VI"

    # one book, and keep the downloaded PDFs
    ./ncert_bulk_import.py --base-url http://127.0.0.1:18000 \
        --cookie-file ~/.eduviz-session --book fecu1 --keep-pdfs
"""

from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ncert_catalog import (  # noqa: E402
    DEFAULT_BASE_URL,
    NcertBook,
    NcertSection,
    discover_chapters,
    load_catalog,
    section_url,
)

TERMINAL_JOB_STATUSES = {"completed", "failed", "stopped"}


class ScriptError(RuntimeError):
    pass


class _ChainedBody:
    """File-like wrapper over ``preamble + file + epilogue``.

    ``http.client`` streams a request body by calling ``read(n)``, so this lets a
    multi-part upload send a large file straight off disk instead of building the
    whole body in memory.
    """

    def __init__(self, preamble: bytes, handle, epilogue: bytes, chunk: int = 1 << 20):
        self._parts = [io.BytesIO(preamble), handle, io.BytesIO(epilogue)]
        self._chunk = chunk

    def read(self, size: int = -1) -> bytes:
        want = size if size and size > 0 else self._chunk
        while self._parts:
            data = self._parts[0].read(want)
            if data:
                return data
            self._parts.pop(0)
        return b""


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #


class ApiClient:
    """Minimal JSON/multipart client for edu_viz, stdlib only."""

    def __init__(self, base_url: str, cookie: str | None, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self.timeout = timeout

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if extra:
            headers.update(extra)
        return headers

    def _open(self, req: request.Request):
        try:
            return request.urlopen(req, timeout=self.timeout)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:400]
            raise ScriptError(
                f"HTTP {exc.code} {req.get_method()} {req.full_url}: {detail}"
            ) from exc
        except error.URLError as exc:
            raise ScriptError(f"cannot reach {req.full_url}: {exc.reason}") from exc

    def get_json(self, path: str, params: dict | None = None):
        url = self.base_url + path
        if params:
            url += "?" + parse.urlencode(params)
        with self._open(request.Request(url, headers=self._headers(), method="GET")) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ScriptError(f"expected JSON from GET {path}, got: {body[:200]!r}") from exc

    def post_json(self, path: str, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.base_url + path,
            data=body,
            headers=self._headers({"Content-Type": "application/json"}),
            method="POST",
        )
        with self._open(req) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def post_form(self, path: str, fields: dict[str, str]):
        body = parse.urlencode(fields).encode("utf-8")
        req = request.Request(
            self.base_url + path,
            data=body,
            headers=self._headers({"Content-Type": "application/x-www-form-urlencoded"}),
            method="POST",
        )
        with self._open(req) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def post_file(self, path: str, filename: str, content: bytes,
                  field_name: str = "source_file"):
        """multipart/form-data upload.

        The field name must match the FastAPI parameter: the bulk endpoints
        declare ``source_file: UploadFile = File(...)``, so the part is
        ``source_file``, not ``file``.
        """
        boundary = f"----eduviz{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        req = request.Request(
            self.base_url + path,
            data=body,
            headers=self._headers(
                {
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Content-Length": str(len(body)),
                    "X-Requested-With": "fetch",  # ask the endpoint for JSON
                }
            ),
            method="POST",
        )
        with self._open(req) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScriptError(f"expected JSON from {path}, got: {raw[:200]!r}") from exc

    def post_file_path(self, path: str, file_path: Path,
                       field_name: str = "source_file",
                       fields: dict[str, str] | None = None):
        """Upload a file from disk without reading it all into memory.

        A whole-book ZIP can be well over 100 MB, so the body is streamed from a
        real file object (urllib sends file-like data in blocks) with an explicit
        Content-Length. ``fields`` become additional form parts (the bulk
        endpoint takes ``folder_id`` as a Form field, not a query param).
        """
        boundary = f"----eduviz{uuid.uuid4().hex}"
        size = file_path.stat().st_size
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/zip"
        preamble = "".join(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            for key, value in (fields or {}).items()
        ) + (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{file_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        )
        preamble_bytes = preamble.encode()
        epilogue = f"\r\n--{boundary}--\r\n".encode()
        total = len(preamble_bytes) + size + len(epilogue)

        handle = file_path.open("rb")
        try:
            req = request.Request(
                self.base_url + path,
                data=_ChainedBody(preamble_bytes, handle, epilogue),
                headers=self._headers(
                    {
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Content-Length": str(total),
                        "X-Requested-With": "fetch",
                    }
                ),
                method="POST",
            )
            with self._open(req) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        finally:
            handle.close()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScriptError(f"expected JSON from {path}, got: {raw[:200]!r}") from exc


def resolve_cookie(args: argparse.Namespace) -> str | None:
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        text = Path(args.cookie_file).expanduser().read_text(encoding="utf-8").strip()
        if not text:
            raise ScriptError(f"cookie file {args.cookie_file} is empty")
        return text
    if args.user_email:
        return mint_cookie(args)
    return None


def mint_cookie(args: argparse.Namespace) -> str:
    """Sign a session cookie with the app's own signer.

    Requires the edu_viz package to be importable (run from a checkout) and the
    app secret, because the session cookie is an itsdangerous-signed blob.
    """
    secret = args.secret_key or os.environ.get("SECRET_KEY")
    if not secret:
        raise ScriptError("--user-email needs --secret-key (or SECRET_KEY in the env)")
    os.environ.setdefault("SECRET_KEY", secret)

    repo_root = Path(args.app_root).resolve()
    sys.path.insert(0, str(repo_root))
    try:
        from app.core.db import SessionLocal
        from app.models import User
        from app.services.session import sign_session
    except Exception as exc:  # noqa: BLE001
        raise ScriptError(
            f"cannot import the app from {repo_root} to mint a cookie: {exc}"
        ) from exc

    db = SessionLocal()
    try:
        user = (
            db.query(User).filter(User.email == args.user_email).first()
        )
        if user is None:
            raise ScriptError(f"no user with email {args.user_email!r}")
        token = sign_session(user_id=user.id)
    finally:
        db.close()
    return f"eduviz_session={token}"


# --------------------------------------------------------------------------- #
# edu_viz objects
# --------------------------------------------------------------------------- #


def folder_slug(value: str) -> str:
    """Folder names must match ^[a-zA-Z0-9_]+$ (validated by POST /api/v1/folders)."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in value.strip())
    return safe.strip("_") or "Unnamed"


def list_child_folders(client: ApiClient, parent_id: str | None) -> list[dict]:
    """GET /api/v1/folders lists ROOT folders only; children come from
    /api/v1/folders/{id}/subfolders."""
    if parent_id is None:
        return client.get_json("/api/v1/folders")
    return client.get_json(f"/api/v1/folders/{parent_id}/subfolders")


def ensure_folder(client: ApiClient, name: str, parent_id: str | None) -> str:
    slug = folder_slug(name)
    for folder in list_child_folders(client, parent_id):
        if folder.get("name") == slug:
            return folder["id"]
    try:
        created = client.post_json(
            "/api/v1/folders", {"name": slug, "parent_id": parent_id}
        )
        return created["id"]
    except ScriptError:
        # 409 = created concurrently (or a stale listing). Re-list and reuse
        # rather than failing the whole import.
        for folder in list_child_folders(client, parent_id):
            if folder.get("name") == slug:
                return folder["id"]
        raise


# Deck creation is deliberately NOT done here. The bulk pipeline names each deck
# from the title it derives from the PDF's content, and the single-deck endpoint
# clears the deck's folder, so pre-creating a "book" deck does not survive. One
# ZIP per book lets the app create one content-titled deck per chapter, filed
# under the folder passed to /bulk-ai-upload/start.


# --------------------------------------------------------------------------- #
# download + upload
# --------------------------------------------------------------------------- #


def fetch_pdf(url: str, timeout: float = 120.0, attempts: int = 3) -> bytes | None:
    """Download a PDF, retrying transport failures.

    Returns None only when the resource is definitively absent (404/410) or the
    body is not a PDF. ncert.nic.in resets connections sporadically, so a single
    failure must not be read as 'missing'.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            return data if data[:5] == b"%PDF-" else None
        except error.HTTPError as exc:
            if exc.code in (404, 410):
                return None
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt < attempts:
            time.sleep(1.5 * attempt)
    raise ScriptError(f"could not download {url} after {attempts} attempts: {last}")


def download_chapter(book: NcertBook, section: NcertSection, base_url: str,
                     timeout: float) -> bytes | None:
    """Try the deterministic PDF path first, then the chapter page's own link."""
    import re

    if section.number is not None:
        candidate = f"{base_url.rstrip('/')}/textbook/pdf/{book.code}{section.number:02d}.pdf"
        data = fetch_pdf(candidate, timeout)
        if data:
            return data

    page_url = section_url(book.code, section, base_url, range_end=book.range_end)
    try:
        with request.urlopen(
            request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout
        ) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except (error.HTTPError, error.URLError, TimeoutError):
        return None

    match = re.search(r"href=[\"']([^\"']*textbook/pdf/[^\"']+\.pdf)[\"']", html, re.I)
    if not match:
        return None
    link = match.group(1)
    if link.startswith("http"):
        pdf_url = link
    elif link.startswith("/"):
        pdf_url = base_url.rstrip("/") + link
    else:
        pdf_url = f"{base_url.rstrip('/')}/{link.lstrip('./')}"
    return fetch_pdf(pdf_url, timeout)


def wait_for_bulk(client: ApiClient, bulk_id: str, poll_seconds: float,
                  max_wait: float) -> dict:
    """Poll one bulk upload until it reaches a terminal state."""
    deadline = time.time() + max_wait
    last: dict = {}
    while time.time() < deadline:
        last = client.get_json(f"/api/v1/bulk-ai-upload/{bulk_id}")
        status = (last.get("status") or "").lower()
        if status in TERMINAL_JOB_STATUSES:
            return last
        time.sleep(poll_seconds)
    return last


# --------------------------------------------------------------------------- #
# state (resume support)
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download NCERT textbooks and import them into edu_viz via its bulk API"
    )
    parser.add_argument("--base-url", required=True,
                        help="edu_viz base URL, e.g. http://127.0.0.1:18000")
    parser.add_argument("--cookie", help="Raw session cookie value")
    parser.add_argument("--cookie-file", help="File containing the session cookie")
    parser.add_argument("--user-email",
                        help="Mint a session cookie for this user (needs --secret-key)")
    parser.add_argument("--secret-key", help="App SECRET_KEY, for --user-email")
    parser.add_argument("--app-root", default="/opt/edu_viz",
                        help="Path to the edu_viz checkout, for --user-email")

    parser.add_argument("--ncert-base-url", default=DEFAULT_BASE_URL,
                        help="Where to download textbooks from")
    parser.add_argument("--class", dest="classes", action="append", default=[],
                        help='Class name, e.g. "Class VI"; repeatable')
    parser.add_argument("--subject", dest="subjects", action="append", default=[],
                        help='Subject name, e.g. "Science"; repeatable')
    parser.add_argument("--book", dest="books", action="append", default=[],
                        help="NCERT book code, e.g. fecu1; repeatable")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N books (0 = no limit)")
    parser.add_argument("--max-chapters", type=int, default=0,
                        help="Upload at most N chapters per book (0 = all)")
    parser.add_argument("--max-book-mb", type=float, default=450.0,
                        help="Refuse a book whose zip exceeds this (server caps at "
                             "MAX_BULK_UPLOAD_MB, 500 by default)")

    parser.add_argument("--workdir", default=None,
                        help="Where to cache downloads (default: a temp dir)")
    parser.add_argument("--keep-pdfs", action="store_true",
                        help="Keep downloaded PDFs in --workdir")
    parser.add_argument("--state-file", default=None,
                        help="Resume state JSON (default: <workdir>/state.json)")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--max-wait", type=float, default=3600.0,
                        help="Give up waiting for one chapter after this many seconds")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan and download nothing; just report what would happen")
    parser.add_argument("--no-wait", action="store_true",
                        help="Upload and move on without waiting for generation")
    return parser.parse_args()


def plan_books(catalog, args) -> list:
    wanted_classes = {c.strip().lower() for c in args.classes}
    wanted_subjects = {s.strip().lower() for s in args.subjects}
    wanted_codes = set(args.books)

    planned = []
    for ncert_class in catalog:
        if wanted_classes and ncert_class.name.lower() not in wanted_classes:
            continue
        for subject in ncert_class.subjects:
            if wanted_subjects and subject.name.lower() not in wanted_subjects:
                continue
            for book in subject.books:
                if wanted_codes and book.code not in wanted_codes:
                    continue
                planned.append((ncert_class, subject, book))
    if args.limit:
        planned = planned[: args.limit]
    return planned


def main() -> int:
    args = parse_args()

    print(f"[1/3] Reading the NCERT catalogue from {args.ncert_base_url} ...")
    try:
        catalog = load_catalog(base_url=args.ncert_base_url, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED to read the catalogue: {exc}", file=sys.stderr)
        return 2

    planned = plan_books(catalog, args)
    total_books = sum(len(s.books) for c in catalog for s in c.subjects)
    print(
        f"  catalogue: {len(catalog)} classes, {total_books} English books; "
        f"selection: {len(planned)} book(s)"
    )
    if not planned:
        print("Nothing matched the filters.", file=sys.stderr)
        return 1

    for ncert_class, subject, book in planned[:15]:
        print(f"   - {ncert_class.name:<12} {subject.name:<34} {book.title} [{book.code}]")
    if len(planned) > 15:
        print(f"   ... and {len(planned) - 15} more")

    if args.dry_run:
        print("\n--dry-run: stopping before any download or upload.")
        return 0

    cookie = resolve_cookie(args)
    if not cookie:
        print(
            "No credentials. Pass --cookie/--cookie-file, or --user-email with "
            "--secret-key.\nThe PDF bulk endpoints require the app session cookie "
            "(the bulk-import API key only covers /api/v1/import/*).",
            file=sys.stderr,
        )
        return 2
    client = ApiClient(args.base_url, cookie, timeout=args.timeout)

    workdir = Path(args.workdir).expanduser() if args.workdir else Path(
        tempfile.mkdtemp(prefix="ncert-import-")
    )
    workdir.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state_file) if args.state_file else workdir / "state.json"
    state = load_state(state_path)
    print(f"  workdir: {workdir}")

    print(f"\n[2/3] Downloading and uploading {len(planned)} book(s) ...")
    totals = {"books": 0, "chapters": 0, "failed": 0, "skipped": 0}

    for index, (ncert_class, subject, book) in enumerate(planned, start=1):
        prefix = f"[{index}/{len(planned)}] {book.title} [{book.code}]"
        print(f"\n{prefix}")

        try:
            chapters = discover_chapters(
                book.code, book.range_end,
                base_url=args.ncert_base_url,
                page_timeout=args.timeout,
                probe_timeout=args.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  could not enumerate the book's chapters: {exc}")
            totals["failed"] += 1
            continue

        if not chapters:
            print("  no chapter PDFs found; skipping")
            totals["failed"] += 1
            continue
        if args.max_chapters:
            chapters = chapters[: args.max_chapters]

        book_key = book.code
        if book_key in state and state[book_key].get("status") == "completed":
            print(f"  {len(chapters)} chapter(s) found; already imported, skipping book")
            totals["skipped"] += len(chapters)
            continue
        print(f"  {len(chapters)} chapter(s) found")

        try:
            class_folder = ensure_folder(client, ncert_class.name, None)
            subject_folder = ensure_folder(client, subject.name, class_folder)
        except ScriptError as exc:
            print(f"  could not set up folders: {exc}")
            totals["failed"] += 1
            continue

        # Download every chapter first, then ship the book as ONE zip. One zip per
        # book means one bulk job per book (instead of one per chapter) and the
        # app creates one content-titled deck per chapter, filed under the folder.
        book_dir = workdir / book.code
        book_dir.mkdir(parents=True, exist_ok=True)
        pdf_paths: list[Path] = []
        missing = 0
        for chapter in chapters:
            pdf_path = book_dir / f"{book.code}{chapter.number:02d}.pdf"
            if not pdf_path.exists():
                try:
                    payload = download_chapter(
                        book, chapter, args.ncert_base_url, args.timeout
                    )
                except ScriptError as exc:
                    print(f"    - {chapter.label}: {exc}")
                    missing += 1
                    continue
                if payload is None:
                    print(f"    - {chapter.label}: PDF unavailable")
                    missing += 1
                    continue
                pdf_path.write_bytes(payload)
            pdf_paths.append(pdf_path)

        if not pdf_paths:
            print("  nothing downloadable; skipping book")
            totals["failed"] += 1
            continue

        total_mb = sum(p.stat().st_size for p in pdf_paths) / (1024 * 1024)
        zip_path = workdir / f"{book.code}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
            for pdf_path in pdf_paths:
                archive.write(pdf_path, arcname=pdf_path.name)
        zip_mb = zip_path.stat().st_size / (1024 * 1024)
        print(f"  zipped {len(pdf_paths)} chapter(s): {total_mb:.1f} MiB -> {zip_mb:.1f} MiB")
        if zip_mb > args.max_book_mb:
            print(
                f"  refusing to upload: {zip_mb:.1f} MiB exceeds --max-book-mb "
                f"{args.max_book_mb} (the server caps at MAX_BULK_UPLOAD_MB)"
            )
            totals["failed"] += 1
            continue

        try:
            result = client.post_file_path(
                "/api/v1/bulk-ai-upload/start",
                zip_path,
                fields={"folder_id": subject_folder},
            )
        except ScriptError as exc:
            print(f"  upload failed: {exc}")
            totals["failed"] += 1
            continue

        bulk_id = result.get("id") or result.get("bulk_upload_id")
        totals["chapters"] += len(pdf_paths)
        state[book_key] = {
            "status": "uploaded",
            "bulk_upload_id": bulk_id,
            "folder_id": subject_folder,
            "chapters": len(pdf_paths),
            "deck_ids": result.get("deck_ids") or [],
        }
        save_state(state_path, state)

        if args.no_wait or not bulk_id:
            print(f"  uploaded as bulk {bulk_id}; not waiting")
        else:
            final = wait_for_bulk(client, bulk_id, args.poll_seconds, args.max_wait)
            status = (final.get("status") or "unknown")
            cards = (
                f"{final.get('flashcards_generated', 0)} flashcards + "
                f"{final.get('mcqs_generated', 0)} mcqs"
            )
            print(f"  bulk {bulk_id}: {status}, {cards}")
            if status == "completed":
                state[book_key]["status"] = "completed"
            else:
                totals["failed"] += 1
                state[book_key]["status"] = status
            save_state(state_path, state)

        if missing:
            print(f"  ({missing} chapter PDF(s) were unavailable and are not in the zip)")
        if not args.keep_pdfs:
            zip_path.unlink(missing_ok=True)
            for pdf_path in pdf_paths:
                pdf_path.unlink(missing_ok=True)
            book_dir.rmdir()

        totals["books"] += 1

    print(
        f"\n[3/3] Done. books={totals['books']} chapters_uploaded={totals['chapters']} "
        f"skipped={totals['skipped']} failed={totals['failed']}"
    )
    print(f"state: {state_path}")
    if not args.keep_pdfs:
        print("downloaded PDFs and zips were deleted after upload (pass --keep-pdfs)")
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
