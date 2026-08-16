/**
 * Peer benchmarking that shows a hospital where it stands without showing it
 * anyone else's numbers.
 *
 * The CCR-vs-peers view is the one feature that legitimately reads across
 * organizations, so it is the one place tenant isolation cannot simply say no.
 * What makes it safe is a distinction worth stating plainly:
 *
 *   * A **percentile rank** is a fact about the subject. "You are at the 30th
 *     percentile of Kansas CAHs" reveals nothing about any individual peer, at
 *     any cohort size. This is always safe to show.
 *
 *   * A **cohort statistic** is a fact about the peers. A median is somebody's
 *     data, and on a small sample it can be exactly one hospital's number.
 *     This needs rules.
 *
 * So the rank is the primary output and the band is the extra, released only
 * when the cohort is large enough to hide an individual inside it.
 *
 * Three disclosure traps this closes:
 *
 *   1. **Min and max are always a specific hospital's value.** A range of
 *      "0.21 to 0.68" publishes two real records verbatim. They are never
 *      returned, and there is a test asserting the response has no such field.
 *
 *   2. **An order statistic on a small sample is an individual record.** The
 *      median of five values *is* the third hospital's number exactly. So
 *      quartiles need a higher floor than the rank does, and published values
 *      are rounded to a coarse grid so they do not reproduce a raw figure.
 *
 *   3. **Uniform cohorts leak everyone at once.** If every peer reports the
 *      same value, publishing the median publishes all of them. The band is
 *      withheld.
 *
 * A fourth trap cannot be closed in this function, and the caller must handle
 * it: **differencing**. Offering "Kansas CAHs" (n=6) alongside "Kansas CAHs
 * under 25 beds" (n=5) lets anyone subtract one from the other and isolate a
 * single hospital. Cohorts must come from a fixed, pre-approved set — never
 * from filters the client composes. See `assertPredefinedCohort`.
 */

"use strict";

/** Peers needed before any cross-org number is released at all. */
const MIN_COHORT = 5;

/**
 * Peers needed before quartiles are released.
 *
 * Higher than MIN_COHORT because quartiles are order statistics: on a small
 * sample they land on a single record rather than summarizing a spread.
 */
const MIN_COHORT_FOR_QUARTILES = 11;

/** Ratios are published to two decimals — coarse enough not to echo a raw figure. */
const DEFAULT_PRECISION = 2;

const DISCLOSURE = Object.freeze({
  BAND: "band",           // rank + quartile band
  RANK_ONLY: "rank_only", // rank only; cohort too small or too uniform for a band
  NONE: "none",           // not enough peers to say anything
});

function round(value, precision) {
  const factor = 10 ** precision;
  return Math.round(value * factor) / factor;
}

function numericAscending(values) {
  return values
    .filter((v) => typeof v === "number" && Number.isFinite(v))
    .sort((a, b) => a - b);
}

/**
 * Linear-interpolated quantile (the "type 7" definition, as used by R and numpy).
 *
 * Interpolation matters here beyond correctness: a value between two records
 * is not either record.
 */
function quantile(sorted, q) {
  if (sorted.length === 0) return null;
  if (sorted.length === 1) return sorted[0];
  const position = (sorted.length - 1) * q;
  const lower = Math.floor(position);
  const upper = Math.ceil(position);
  if (lower === upper) return sorted[lower];
  return sorted[lower] + (position - lower) * (sorted[upper] - sorted[lower]);
}

/**
 * Where the subject sits among its peers, 0-100.
 *
 * Ties count as half, so a hospital identical to all its peers lands at 50
 * rather than 0 or 100. This is a statement about the subject only.
 */
function percentileRank(subject, peers) {
  const values = numericAscending(peers);
  if (values.length === 0) return null;
  let below = 0;
  let equal = 0;
  for (const v of values) {
    if (v < subject) below += 1;
    else if (v === subject) equal += 1;
  }
  return Math.round(((below + equal / 2) / values.length) * 100);
}

/**
 * Build the peer comparison for one hospital.
 *
 * `peers` must be the *other* hospitals' values — the subject is excluded so
 * that a hospital is never benchmarked against itself, and so the two cohorts
 * a differencing attack would compare are never both available.
 *
 * The returned object is the whole contract with the UI: it carries no peer
 * list, no minimum, and no maximum, so a template cannot render a peer's
 * number by accident.
 */
function buildPeerBenchmark(options) {
  const {
    subjectValue,
    peerValues,
    cohortLabel = "peers",
    minCohort = MIN_COHORT,
    minCohortForQuartiles = MIN_COHORT_FOR_QUARTILES,
    precision = DEFAULT_PRECISION,
    higherIsBetter = false,
  } = options || {};

  const peers = numericAscending(peerValues || []);
  const subject =
    typeof subjectValue === "number" && Number.isFinite(subjectValue) ? subjectValue : null;

  const base = {
    cohortLabel,
    cohortSize: peers.length,
    yourValue: subject === null ? null : round(subject, precision),
    percentileRank: null,
    band: null,
    disclosure: DISCLOSURE.NONE,
    note: null,
    higherIsBetter,
  };

  if (subject === null) {
    return Object.assign(base, { note: "No value reported for this hospital yet." });
  }

  if (peers.length < minCohort) {
    // Below the floor even a rank is thin, and it invites the reader to
    // reconstruct the handful of hospitals involved.
    return Object.assign(base, {
      note:
        `Comparison withheld: only ${peers.length} comparable ${cohortLabel} ` +
        `(at least ${minCohort} are needed to compare without identifying them).`,
    });
  }

  const rank = percentileRank(subject, peers);
  const uniform = peers[0] === peers[peers.length - 1];

  if (peers.length < minCohortForQuartiles || uniform) {
    return Object.assign(base, {
      percentileRank: rank,
      disclosure: DISCLOSURE.RANK_ONLY,
      note: uniform
        ? `Every comparable hospital reports the same value, so no range is shown.`
        : `Showing your position only — ${peers.length} ${cohortLabel} is too few ` +
          `to publish a range without revealing individual hospitals.`,
    });
  }

  return Object.assign(base, {
    percentileRank: rank,
    band: {
      p25: round(quantile(peers, 0.25), precision),
      p50: round(quantile(peers, 0.5), precision),
      p75: round(quantile(peers, 0.75), precision),
    },
    disclosure: DISCLOSURE.BAND,
  });
}

/**
 * Cohorts the app is willing to build, by key.
 *
 * Client-composed filters are what make differencing possible, so the API must
 * accept a key from this list and nothing else. Adding a cohort here is a
 * deliberate act; accepting `?state=KS&beds<25` from a query string is not.
 */
const COHORTS = Object.freeze({
  STATE_PEER_TYPE: "state_peer_type",       // same state, same provider subtype
  NATIONAL_PEER_TYPE: "national_peer_type", // same provider subtype, nationwide
  STATE_ALL: "state_all",                   // every hospital in the state
});

function assertPredefinedCohort(key) {
  const allowed = Object.values(COHORTS);
  if (!allowed.includes(key)) {
    const err = new Error(
      `unknown cohort "${key}" — cohorts must come from the predefined set ` +
      `(${allowed.join(", ")}) so overlapping slices cannot isolate a hospital`,
    );
    err.status = 400;
    err.code = "unknown_cohort";
    throw err;
  }
  return key;
}

module.exports = {
  MIN_COHORT,
  MIN_COHORT_FOR_QUARTILES,
  DEFAULT_PRECISION,
  DISCLOSURE,
  COHORTS,
  buildPeerBenchmark,
  percentileRank,
  quantile,
  assertPredefinedCohort,
};
