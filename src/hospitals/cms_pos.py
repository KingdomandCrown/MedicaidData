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
import re
from dataclasses import dataclass
from typing import Iterable, Iterator

import requests

from .logging_config import get_logger
from .normalize import COL_CATEGORY, COL_STATE, CATEGORY_HOSPITAL

log = get_logger(__name__)

# DKAN metastore: lists every dataset published on data.cms.gov.
METASTORE_ITEMS_URL = "https://data.cms.gov/api/1/metastore/schemas/dataset/items"
# DCAT-US catalog for the main portal, where the POS file is published.
DATA_JSON_URL = "https://data.cms.gov/data.json"
# The Care Compare subsite keeps its own DKAN metastore.
PROVIDER_DATA_ITEMS_URL = (
    "https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items"
)
# data-api base for querying a specific distribution's rows.
DATA_API_BASE = "https://data.cms.gov/data-api/v1/dataset"

# Catalogs to search, in order, each with the shape its payload arrives in.
#
# ``METASTORE_ITEMS_URL`` is listed last and now answers with the portal's
# single-page app rather than JSON — CMS retired that path. Keeping it costs
# one request on a path that already failed and means an environment still
# served by the old API keeps working.
CATALOGS = (
    (DATA_JSON_URL, None, "dcat"),
    (PROVIDER_DATA_ITEMS_URL, {"show-reference-ids": "true"}, "dkan"),
    (METASTORE_ITEMS_URL, {"show-reference-ids": "true"}, "dkan"),
)

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
    distribution_id: str | None  # data-api resource UUID, when the catalog gives one
    title: str  # human title, e.g. the dataset title
    modified: str | None  # last-modified date reported by CMS
    download_url: str | None  # direct CSV URL, when available
    catalog: str | None = None  # which catalog it came from, for provenance

    @property
    def has_data_api(self) -> bool:
        """Whether rows can be queried with server-side filtering.

        A DCAT catalog describes where a file *is*, not a queryable resource,
        so entries from ``data.json`` carry a download URL and no identifier.
        Those are read by downloading the CSV and filtering locally.
        """

        return bool(self.distribution_id)

    @property
    def data_api_url(self) -> str:
        if not self.distribution_id:
            raise LookupError(
                f"{self.title!r} has no data-api identifier — read it from "
                f"{self.download_url or 'no download URL either'}"
            )
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


def _normalize_title(title: str | None) -> str:
    """Lowercase, with every run of punctuation flattened to one space.

    CMS's own rendering of this title varies: a hyphen becomes an en dash, an
    ampersand becomes "and", a colon replaces the dash. A substring match on
    the literal string fails on any of those while a human reads the two
    titles as identical.
    """

    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


# The part of the title that identifies the dataset regardless of how CMS
# renders the rest of it.
POS_TITLE_CORE = "provider of services"


def _matching_datasets(items, dataset_title: str) -> list[dict]:
    """Datasets whose title is the one we want, matched from strict to loose.

    Four tiers, stopping at the first that finds anything: the exact string;
    the same title once punctuation is normalized; a title containing every
    word of ours (so an added "Q1 2026" still matches); and finally any title
    carrying the core phrase. Each step gives up a little precision, so the
    order matters — a stricter match is never passed over for a looser one.
    """

    exact = [it for it in items if it.get("title") == dataset_title]
    if exact:
        return exact

    target = _normalize_title(dataset_title)
    normalized = [(it, _normalize_title(it.get("title"))) for it in items]

    same = [it for it, norm in normalized if norm == target]
    if same:
        return same

    wanted = set(target.split())
    superset = [it for it, norm in normalized if wanted <= set(norm.split())]
    if superset:
        return superset

    return [it for it, norm in normalized if POS_TITLE_CORE in norm]


def _near_miss_titles(payload) -> list[str]:
    """Titles that mention the core phrase, for a failure message.

    When nothing matches, the useful thing is not that the search failed but
    what CMS is calling the dataset now.
    """

    if isinstance(payload, dict):
        items = payload.get("dataset") or []
    elif isinstance(payload, list):
        items = payload
    else:
        return []

    return sorted(
        {
            it.get("title")
            for it in items
            if isinstance(it, dict)
            and POS_TITLE_CORE in _normalize_title(it.get("title"))
            and it.get("title")
        }
    )


def _from_dkan(payload, dataset_title: str, catalog: str) -> list[PosDistribution]:
    """Distributions from a DKAN metastore listing (a bare list of datasets)."""

    if not isinstance(payload, list):
        return []

    out: list[PosDistribution] = []
    for item in _matching_datasets(payload, dataset_title):
        dataset_id = item.get("identifier", "")
        modified = item.get("modified")
        for dist in item.get("distribution", []) or []:
            dist_data = dist.get("data", dist)
            distribution_id = dist_data.get("identifier") or dist.get("identifier")
            if not distribution_id:
                continue
            out.append(
                PosDistribution(
                    dataset_id=dataset_id,
                    distribution_id=distribution_id,
                    title=item.get("title", dataset_title),
                    modified=dist_data.get("modified") or modified,
                    download_url=dist_data.get("downloadURL"),
                    catalog=catalog,
                )
            )
    return out


def _from_dcat(payload, dataset_title: str, catalog: str) -> list[PosDistribution]:
    """Distributions from a DCAT-US catalog (``{"dataset": [...]}``).

    DCAT describes where a file lives rather than exposing a queryable
    resource, so these carry a download URL and no identifier. CSV
    distributions are preferred; anything else is ignored rather than
    downloaded and found to be a PDF.
    """

    if not isinstance(payload, dict):
        return []

    out: list[PosDistribution] = []
    for item in _matching_datasets(payload.get("dataset") or [], dataset_title):
        dataset_id = item.get("identifier", "")
        modified = item.get("modified")
        for dist in item.get("distribution", []) or []:
            url = dist.get("downloadURL") or dist.get("accessURL")
            if not url:
                continue
            media = (dist.get("mediaType") or dist.get("format") or "").lower()
            if media and "csv" not in media and not url.lower().endswith(".csv"):
                continue
            out.append(
                PosDistribution(
                    dataset_id=dataset_id,
                    distribution_id=None,
                    title=item.get("title", dataset_title),
                    modified=dist.get("modified") or modified,
                    download_url=url,
                    catalog=catalog,
                )
            )
    return out


_SHAPES = {"dkan": _from_dkan, "dcat": _from_dcat}


def discover_latest_distribution(
    session: requests.Session | None = None,
    dataset_title: str = POS_DATASET_TITLE,
    catalogs=CATALOGS,
) -> PosDistribution:
    """Find the most recent POS distribution, trying each CMS catalog in turn.

    CMS retired ``/api/1/metastore/`` on the main portal — it answers with the
    site's own HTML now — so a single hard-coded endpoint is a single point of
    failure that has already failed once. Each catalog is tried in order and
    the first one carrying the dataset wins.

    Returns the newest :class:`PosDistribution` (by ``modified`` date). Raises
    :class:`CmsUnavailableError` if no catalog could be reached at all, or
    ``LookupError`` naming every catalog tried if they answered but none held
    the dataset.
    """

    sess = _session(session)
    attempts: list[str] = []
    near_miss: set[str] = set()
    reachable = False

    for url, params, shape in catalogs:
        log.info("Looking for the POS dataset in %s", url)
        try:
            payload = _get_json(url, params, sess)
        except CmsUnavailableError as exc:
            attempts.append(f"{url}: {exc}")
            continue

        reachable = True
        candidates = _SHAPES[shape](payload, dataset_title, url)
        if not candidates:
            near = _near_miss_titles(payload)
            near_miss.update(near)
            attempts.append(
                f"{url}: reachable, but no usable POS distribution"
                + (f" (closest titles: {'; '.join(near[:3])})" if near else "")
            )
            continue

        candidates.sort(key=lambda d: (d.modified or ""), reverse=True)
        latest = candidates[0]
        if latest.title != dataset_title:
            # A loose match found something; say which something, and what
            # else it could have chosen. Silently loading a dataset nobody
            # asked for is worse than not finding one — and CMS publishes
            # several "Provider of Services" files covering different provider
            # systems, so which one won is the fact that decides whether the
            # load is meaningful.
            alternatives = sorted({c.title for c in candidates if c.title != latest.title})
            log.warning(
                "Matched %r for requested %r — verify this is the file you want",
                latest.title,
                dataset_title,
            )
            if alternatives:
                log.warning(
                    "Other datasets also matched: %s — pass dataset_title= to choose one",
                    "; ".join(alternatives),
                )
        log.info(
            "Latest POS distribution: %s (modified=%s, %s)",
            latest.title,
            latest.modified,
            f"id={latest.distribution_id}"
            if latest.has_data_api
            else f"csv={latest.download_url}",
        )
        return latest

    detail = "; ".join(attempts) or "no catalogs configured"
    if not reachable:
        raise CmsUnavailableError(f"no CMS catalog could be reached — {detail}")
    hint = ""
    if near_miss:
        hint = (
            " CMS appears to publish it under a different title now; set "
            "POS_DATASET_TITLE to one of: " + "; ".join(sorted(near_miss)[:5])
        )
    raise LookupError(
        f"POS dataset {dataset_title!r} not found in any CMS catalog — {detail}.{hint}"
    )


def iter_distribution_records(
    distribution: PosDistribution,
    state_usps: str | None = None,
    hospitals_only: bool = True,
    session: requests.Session | None = None,
) -> Iterator[dict]:
    """Read a distribution by whichever route it supports.

    The data-api filters by state server-side, so only the wanted rows cross
    the network. A DCAT catalog entry has no queryable resource behind it —
    only a file — so the whole CSV is streamed and the caller's own filtering
    does the rest. Same records either way.
    """

    if distribution.has_data_api:
        yield from iter_data_api_records(
            distribution,
            state_usps=state_usps,
            hospitals_only=hospitals_only,
            session=session,
        )
        return

    log.info(
        "%s has no data-api resource; streaming the CSV and filtering locally",
        distribution.title,
    )
    yield from download_csv(distribution, session=session)


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
        records = iter_distribution_records(
            distribution,
            state_usps=state_usps,
            hospitals_only=hospitals_only,
            session=session,
        )

    if limit is not None:
        records = itertools.islice(records, limit)
    yield from records
