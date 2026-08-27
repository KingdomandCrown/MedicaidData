"""Downloading the files a discovery run found.

Separate from discovery so a failed download is a retry rather than a re-crawl,
and separate from ingestion so the files land in the same folder every other
MRF has arrived in and go through the same parser.

Two decisions worth stating.

**The CCN goes in the filename.** Everything already in the database was linked
after the fact — by an NPI in the file's metadata, or by matching its hospital
name against the POS file — and 14% of files never linked at all. A file we
downloaded, we already know the owner of, because discovery started from the
hospital. Writing ``ccn-170027_...`` at the front of the name carries that
certainty into ingestion, where it beats both heuristics. It is deliberately
not bare digits: ``170027`` in a filename would be read by the EIN and NPI
parsers, and this is neither.

**Size is capped and content is streamed.** These files run to gigabytes —
Ascension Seton's is 32.6 million rows. A cap that stops a runaway download is
the difference between a slow night and a full disk, and this repository has
already spent one night on a full disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from .logging_config import get_logger

log = get_logger(__name__)

#: Stop a single download here. The largest legitimate MRF seen so far is
#: about 4 GB; past this it is a mirror, a redirect loop, or a mistake.
MAX_BYTES = 8 * 1024 * 1024 * 1024

#: Content types a hospital's MRF actually arrives as, mapped to an extension
#: the parser recognises. Servers are unreliable here, so the URL wins when it
#: carries a usable suffix.
_TYPE_EXT = {
    "application/json": ".json",
    "text/json": ".json",
    "text/csv": ".csv",
    "application/csv": ".csv",
    "text/plain": ".csv",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/x-gzip": ".gz",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
}

_KNOWN_EXT = (".json", ".csv", ".xlsx", ".xlsm", ".xls", ".zip", ".gz", ".txt")

#: What a file actually is, from its first bytes.
#:
#: Neither the URL nor the Content-Type can be trusted here. Lexington Regional
#: publishes a ZIP archive at a ``.csv`` address, and the parser met it as
#: "no recognizable data header in the first 8 rows" — a true statement about a
#: compressed archive, and no help at all. The magic number is the only thing
#: on the wire that cannot be a mistake.
_MAGIC = (
    (b"PK\x03\x04", ".zip"),
    (b"PK\x05\x06", ".zip"),   # an empty archive
    (b"\x1f\x8b", ".gz"),
    (b"%PDF", ".pdf"),
)


def sniff_extension(head: bytes) -> str | None:
    """The real format of a response, or None when the bytes do not say.

    ``.html`` is returned for a page, which is not a format this pipeline wants
    but is very much worth knowing: a hospital serving its 404 with a 200 would
    otherwise be saved as a CSV and fail much later, somewhere less obvious.
    """

    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if text[:1] in (b"{", b"["):
        return ".json"
    if text[:1] == b"<":
        return ".html"
    return None

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str | None, limit: int = 60) -> str:
    slug = _SLUG_RE.sub("-", str(name or "").lower()).strip("-")
    return slug[:limit].strip("-") or "hospital"


def extension_for(url: str, content_type: str | None = None) -> str:
    """The suffix the parser will dispatch on.

    The URL is trusted before the header: a hospital serving a 300 MB JSON file
    as ``text/plain`` is common, and treating it as CSV wastes the download.
    """

    path = unquote(urlparse(str(url or "")).path).lower()
    # ``.json.gz`` and ``.csv.zip`` both occur; keep the pair.
    for outer in (".gz", ".zip"):
        if path.endswith(outer):
            stem = path[: -len(outer)]
            for inner in (".json", ".csv", ".xlsx"):
                if stem.endswith(inner):
                    return inner + outer
            return outer
    for ext in _KNOWN_EXT:
        if path.endswith(ext):
            return ext

    base = str(content_type or "").split(";")[0].strip().lower()
    return _TYPE_EXT.get(base, ".csv")


def filename_for(ccn: str, name: str, url: str, content_type: str | None = None) -> str:
    """``ccn-170027_pratt-regional-medical-center_standardcharges.json``

    The ``ccn-`` prefix is what makes this file self-identifying. Bare digits
    would be read as an EIN or an NPI by the parsers that handle every other
    file in the folder.
    """

    return (
        f"ccn-{str(ccn).strip().upper()}"
        f"_{slugify(name)}"
        f"_standardcharges{extension_for(url, content_type)}"
    )


def existing_download(dest_dir: str, ccn: str, name: str) -> str | None:
    """A file already downloaded for this hospital, whatever its extension.

    Matching on the stem rather than the full name is what makes a resume
    correct: the same URL can yield ``.json`` on one run and ``.csv`` on
    another, depending only on what the server said its content type was.
    """

    stem = f"ccn-{str(ccn).strip().upper()}_{slugify(name)}_standardcharges."
    try:
        entries = os.listdir(dest_dir)
    except OSError:
        return None
    for entry in sorted(entries):
        # A scratch file is a download in progress, not a download.
        if entry.startswith(stem) and not entry.endswith(".part"):
            return os.path.join(dest_dir, entry)
    return None


@dataclass
class Fetched:
    ccn: str
    url: str
    path: str | None = None
    bytes_written: int = 0
    status: str = "ok"          # ok | skipped | too_big | error
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "skipped")


def fetch_one(
    row: dict,
    dest_dir: str,
    *,
    opener,
    max_bytes: int = MAX_BYTES,
    overwrite: bool = False,
) -> Fetched:
    """Download one manifest row into ``dest_dir``.

    ``opener(url) -> (content_type, iterator_of_chunks)``. Injected so the
    tests exercise the size cap and the partial-file cleanup without a network.

    A download that fails part way leaves nothing behind. A truncated MRF
    parses — it just stops early — so a half-file on disk is a hospital that
    looks ingested and is missing most of its prices.
    """

    ccn = str(row.get("ccn") or "").strip()
    url = str(row.get("mrf_url") or "").strip()
    name = row.get("name") or ccn
    result = Fetched(ccn=ccn, url=url)

    if not url:
        result.status = "error"
        result.note = "no mrf_url on this row"
        return result

    # Check the disk before opening the connection. A resume over 2,300
    # hospitals would otherwise make 2,300 requests to hospital web servers to
    # learn what a directory listing already knew. The extension is not part of
    # the question: a file may have been saved as .json on a URL that gives no
    # suffix, and it is still that hospital's file.
    if not overwrite:
        existing = existing_download(dest_dir, ccn, name)
        if existing:
            result.path = existing
            result.status = "skipped"
            result.bytes_written = os.path.getsize(existing)
            result.note = "already downloaded"
            return result

    try:
        content_type, chunks = opener(url)
    except Exception as exc:  # noqa: BLE001 - one bad host must not end the run
        result.status = "error"
        result.note = f"{type(exc).__name__}: {exc}"
        log.warning("CCN %s: %s", ccn, result.note)
        return result

    # Peek at the first chunk before choosing a name. The URL and the header
    # are both claims; the magic number is what the file is.
    chunks = iter(chunks)
    head = b""
    for chunk in chunks:
        if chunk:
            head = chunk
            break

    sniffed = sniff_extension(head[:512])
    if sniffed == ".html":
        result.status = "error"
        result.note = "server returned an HTML page, not a data file"
        log.warning("CCN %s: %s", ccn, result.note)
        return result

    named = filename_for(ccn, name, url, content_type)
    if sniffed and not named.endswith(sniffed):
        # `.json.gz` sniffs as `.gz`; the URL's more specific pair wins. A `.csv`
        # that sniffs as `.zip` does not — there the URL is simply wrong.
        stem = named[: named.rindex("_standardcharges")] + "_standardcharges"
        named = stem + sniffed

    path = os.path.join(dest_dir, named)
    result.path = path

    os.makedirs(dest_dir, exist_ok=True)
    # The pid is in the scratch name because two fetch runs can overlap — a
    # backgrounded job that looks finished, started again. Sharing one ".part"
    # meant whichever finished first renamed it out from under the other, which
    # then died on os.replace with a FileNotFoundError naming a file it had
    # just written itself.
    partial = f"{path}.{os.getpid()}.part"
    written = 0
    try:
        with open(partial, "wb") as handle:
            for chunk in ([head] if head else []) + [None]:
                if chunk is None:
                    break
                written += len(chunk)
                handle.write(chunk)
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(
                        f"exceeded {max_bytes} bytes; stopped rather than fill the disk"
                    )
                handle.write(chunk)
    except Exception as exc:  # noqa: BLE001
        _remove(partial)
        result.status = "too_big" if isinstance(exc, ValueError) else "error"
        result.note = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
        log.warning("CCN %s: %s", ccn, result.note)
        return result

    if written == 0:
        _remove(partial)
        result.status = "error"
        result.note = "server returned an empty body"
        return result

    os.replace(partial, path)
    result.bytes_written = written
    log.info("CCN %s: %s (%.1f MB)", ccn, os.path.basename(path), written / 1e6)
    return result


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def requests_opener(session=None, timeout: int = 60):
    """A real network opener, kept out of :func:`fetch_one` so it stays testable."""

    import requests

    sess = session or requests.Session()
    sess.headers.update(
        {
            # Some hospital sites refuse a default python-requests agent. This
            # identifies the crawler honestly and gives a contact path.
            "User-Agent": "MinervaAI-MRF-Fetch/1.0 (+price transparency research)",
            "Accept": "*/*",
        }
    )

    def opener(url: str):
        response = sess.get(url, stream=True, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        return (
            response.headers.get("Content-Type"),
            response.iter_content(chunk_size=1 << 20),
        )

    return opener
