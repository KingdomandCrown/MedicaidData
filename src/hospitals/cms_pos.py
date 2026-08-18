"""Fetch the CMS Provider of Services (POS) file.

CMS publishes the POS "Hospital & Non-Hospital Facilities" file quarterly on
data.cms.gov. There are two ways in:

1. **data-api** (preferred): a JSON API that supports server-side column
   filtering and pagination, so we can ask only for the rows in our target
   state instead of downloading the whole ~100 MB file. Each quarterly edition
   is a distinct "distribution" with its own UUID; we discover the latest one
   through the DKAN metastore.

2. **CSV distribution**: the same data as a downloadable CSV. Used as a
   fallback and for the ``--input-file`` / offline path.

Both surfaces use the same uppercase POS column names, so downstream code sees
a uniform stream of ``dict`` records regardless of which path produced them.
"""

from __future__ import annotations

import csv
import io
import itertools
from dataclasses import dataclass
from typing import Iterable, Iterator

import requests

from .logging_config import get_logger
from .normalize import COL_CATEGORY, COL_STATE, CATEGORY_HOSPITAL

log = get_logger(__name__)

# DKAN metastore: lists every dataset published on data.cms.gov.
METASTORE_ITEMS_URL = "https://data.cms.gov/api/1/metastore/schemas/dataset/items"
# data-api base for querying a specific distribution's rows.
DATA_API_BASE = "https://data.cms.gov/data-api/v1/dataset"

# Title of the POS dataset series we care about. CMS keeps this stable across
# quarterly editions.
POS_DATASET_TITLE = "Provider of Services File - Hospital & Non-Hospital Facilities"

DEFAULT_TIMEOUT = 120
DEFAULT_PAGE_SIZE = 1000
USER_AGENT = "hospitals-kb/0.1 (+https://github.com/KingdomandCrown/MedicaidData)"


@dataclass
class PosDistribution:
    """A single quarterly edition of the POS file."""

    dataset_id: str  # metastore dataset identifier
    distribution_id: str  # data-api resource UUID
    title: str  # human title, e.g. the dataset title
    modified: str | None  # last-modified date reported by CMS
    download_url: str | None  # direct CSV URL, when available

    @property
    def data_api_url(self) -> str:
        return f"{DATA_API_BASE}/{self.distribution_id}/data"


class CmsUnavailableError(RuntimeError):
    """Raised when CMS endpoints cannot be reached (network/policy blocked)."""


def _session(session: requests.Session | None = None) -> requests.Session:
    """A session that identifies itself and asks for JSON.

    ``setdefault`` was the obvious way to write this and did nothing: a fresh
    ``requests.Session`` already carries ``Accept: */*`` and its own
    ``User-Agent``, so the key was never absent and ours was never sent. CMS
    duly answered the metastore path with the portal's single-page app — a 200
    carrying HTML — because a client asking for anything gets the thing meant
    for browsers.

    Replace requests' defaults, but leave alone any header a caller set
    deliberately.
    """

    sess = session or requests.Session()
    stock = requests.utils.default_headers()
    for name, value in (("User-Agent", USER_AGENT), ("Accept", "application/json")):
        if sess.headers.get(name) in (None, stock.get(name)):
            sess.headers[name] = value
    return sess


def _get_json(url: str, params: dict | None, session: requests.Session):
    try:
        resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as exc:  # network / proxy / TLS
        raise CmsUnavailableError(f"could not reach {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise CmsUnavailableError(
            f"{url} returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        # A 200 carrying HTML is the signature of something answering *for*
        # CMS — a captive portal, a corporate proxy's block page, a CDN
        # challenge. The JSONDecodeError alone ("Expecting value: line 1
        # column 1") says only that the first character was not a brace, which
        # is the least useful true statement available. What the body actually
        # was is the whole diagnosis.
        content_type = resp.headers.get("Content-Type", "unknown")
        body = " ".join(resp.text[:300].split())
        raise CmsUnavailableError(
            f"{url} returned HTTP {resp.status_code} with {content_type}, "
            f"not JSON — something is answering for CMS. Body starts: {body!r}"
        ) from exc


def discover_latest_distribution(
    session: requests.Session | None = None,
    dataset_title: str = POS_DATASET_TITLE,
) -> PosDistribution:
    """Find the most recent POS distribution via the DKAN metastore.

    Returns the newest :class:`PosDistribution` (by ``modified`` date). Raises
    :class:`CmsUnavailableError` if CMS cannot be reached, or ``LookupError``
    if the dataset series or a usable distribution cannot be found.
    """

    sess = _session(session)
    log.info("Discovering latest POS distribution from CMS metastore")
    items = _get_json(METASTORE_ITEMS_URL, {"show-reference-ids": "true"}, sess)

    matches = [it for it in items if it.get("title") == dataset_title]
    if not matches:
        # Fall back to a looser contains-match in case CMS retitles slightly.
        needle = dataset_title.lower()
        matches = [it for it in items if needle in (it.get("title") or "").lower()]
    if not matches:
        raise LookupError(f"POS dataset series not found: {dataset_title!r}")

    # A dataset series typically exposes each quarter as a distribution.
    candidates: list[PosDistribution] = []
    for item in matches:
        dataset_id = item.get("identifier", "")
        modified = item.get("modified")
        for dist in item.get("distribution", []) or []:
            dist_data = dist.get("data", dist)
            distribution_id = dist_data.get("identifier") or dist.get("identifier")
            if not distribution_id:
                continue
            candidates.append(
                PosDistribution(
                    dataset_id=dataset_id,
                    distribution_id=distribution_id,
                    title=item.get("title", dataset_title),
                    modified=dist_data.get("modified") or modified,
                    download_url=dist_data.get("downloadURL"),
                )
            )

    if not candidates:
        raise LookupError("no distributions found for POS dataset series")

    candidates.sort(key=lambda d: (d.modified or ""), reverse=True)
    latest = candidates[0]
    log.info(
        "Latest POS distribution: %s (modified=%s, id=%s)",
        latest.title,
        latest.modified,
        latest.distribution_id,
    )
    return latest


def iter_data_api_records(
    distribution: PosDistribution,
    state_usps: str | None = None,
    hospitals_only: bool = True,
    session: requests.Session | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Iterator[dict]:
    """Stream records from the data-api with server-side filtering.

    When ``state_usps`` is provided, CMS filters by state for us so we only
    download the rows we need. Pagination continues until a short page is
    returned.
    """

    sess = _session(session)
    filters: dict[str, str] = {}
    if state_usps:
        filters[f"filter[{COL_STATE}]"] = state_usps.upper()
    if hospitals_only:
        filters[f"filter[{COL_CATEGORY}]"] = CATEGORY_HOSPITAL

    offset = 0
    total = 0
    while True:
        params = {"size": page_size, "offset": offset, **filters}
        page = _get_json(distribution.data_api_url, params, sess)
        if not isinstance(page, list):
            # Some deployments wrap rows under a "data" key.
            page = page.get("data", []) if isinstance(page, dict) else []
        if not page:
            break
        for row in page:
            yield row
        total += len(page)
        log.debug("Fetched %d POS rows (offset=%d)", total, offset)
        if len(page) < page_size:
            break
        offset += page_size
    log.info("data-api returned %d rows", total)


def iter_csv_records(path_or_file) -> Iterator[dict]:
    """Yield records from a POS CSV file path or open text file object.

    The CSV uses the same uppercase POS column headers as the data-api.
    """

    if hasattr(path_or_file, "read"):
        yield from _iter_csv_stream(path_or_file)
        return
    with open(path_or_file, "r", encoding="utf-8-sig", newline="") as fh:
        yield from _iter_csv_stream(fh)


def _iter_csv_stream(fh) -> Iterator[dict]:
    reader = csv.DictReader(fh)
    count = 0
    for row in reader:
        count += 1
        yield row
    log.info("Read %d rows from CSV", count)


def download_csv(
    distribution: PosDistribution,
    session: requests.Session | None = None,
) -> Iterator[dict]:
    """Download a distribution's CSV and yield its records (streaming)."""

    if not distribution.download_url:
        raise LookupError("distribution has no downloadURL for CSV access")
    sess = _session(session)
    log.info("Downloading POS CSV from %s", distribution.download_url)
    try:
        resp = sess.get(distribution.download_url, timeout=DEFAULT_TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise CmsUnavailableError(
            f"could not download {distribution.download_url}: {exc}"
        ) from exc
    if resp.status_code >= 400:
        raise CmsUnavailableError(
            f"download returned HTTP {resp.status_code} for {distribution.download_url}"
        )
    resp.encoding = resp.encoding or "utf-8"
    lines = resp.iter_lines(decode_unicode=True)
    text_stream = _lines_to_stream(lines)
    yield from _iter_csv_stream(text_stream)


def _lines_to_stream(lines: Iterable[str]) -> io.StringIO:
    # csv.DictReader wants an iterable of lines; wrap the streamed lines.
    class _LineIterFile:
        def __init__(self, it):
            self._it = iter(it)

        def __iter__(self):
            return self._it

    return _LineIterFile(lines)  # type: ignore[return-value]


def fetch_records(
    state_usps: str | None = None,
    hospitals_only: bool = True,
    input_file: str | None = None,
    session: requests.Session | None = None,
    limit: int | None = None,
) -> Iterator[dict]:
    """High-level entry point: yield raw POS records for the requested state.

    Resolution order:
      * ``input_file`` given -> read that local CSV (offline path).
      * otherwise -> discover the latest CMS distribution and stream it from
        the data-api with server-side filtering.
    """

    if input_file:
        log.info("Reading POS records from local file: %s", input_file)
        records = iter_csv_records(input_file)
    else:
        distribution = discover_latest_distribution(session=session)
        records = iter_data_api_records(
            distribution,
            state_usps=state_usps,
            hospitals_only=hospitals_only,
            session=session,
        )

    if limit is not None:
        records = itertools.islice(records, limit)
    yield from records
