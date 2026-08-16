/**
 * Tests for peer benchmarking without peer disclosure.
 * Run: node --test access/test/benchmark.test.js
 *
 * The property that matters most is negative: whatever the cohort looks like,
 * the response must never carry an individual hospital's number.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");

const B = require(path.join(__dirname, "..", "benchmark.js"));

const many = (n, fn) => Array.from({ length: n }, (_, i) => fn(i));

// A realistic 12-hospital CCR cohort.
const COHORT_12 = [0.21, 0.27, 0.29, 0.33, 0.35, 0.38, 0.41, 0.44, 0.49, 0.55, 0.61, 0.68];

// --- percentile rank: a fact about the subject ----------------------------

test("percentile rank places the subject in the cohort", () => {
  const peers = [10, 20, 30, 40];
  assert.equal(B.percentileRank(5, peers), 0);
  assert.equal(B.percentileRank(25, peers), 50);
  assert.equal(B.percentileRank(50, peers), 100);
});

test("ties count as half, so matching every peer lands mid-cohort", () => {
  assert.equal(B.percentileRank(10, [10, 10, 10, 10]), 50);
});

test("percentile rank ignores non-numeric peers", () => {
  assert.equal(B.percentileRank(25, [10, null, 20, "n/a", 30, undefined, 40]), 50);
});

// --- quantiles ------------------------------------------------------------

test("quantiles interpolate between records rather than landing on one", () => {
  // Between 20 and 30, so equal to neither.
  assert.equal(B.quantile([10, 20, 30, 40], 0.5), 25);
});

test("quantile of an empty set is null, not zero", () => {
  assert.equal(B.quantile([], 0.5), null);
});

// --- the release rules ----------------------------------------------------

test("a healthy cohort gets rank and a band", () => {
  const out = B.buildPeerBenchmark({
    subjectValue: 0.31,
    peerValues: COHORT_12,
    cohortLabel: "Kansas critical access hospitals",
  });

  assert.equal(out.disclosure, B.DISCLOSURE.BAND);
  assert.equal(out.cohortSize, 12);
  assert.equal(out.yourValue, 0.31);
  assert.equal(typeof out.percentileRank, "number");
  assert.equal(typeof out.band.p50, "number");
});

test("below the floor nothing crosses the tenant line", () => {
  const out = B.buildPeerBenchmark({ subjectValue: 0.4, peerValues: [0.3, 0.35, 0.5, 0.6] });

  assert.equal(out.disclosure, B.DISCLOSURE.NONE);
  assert.equal(out.percentileRank, null);
  assert.equal(out.band, null);
  assert.match(out.note, /only 4 comparable/);
});

test("a mid-sized cohort gets the rank but no band", () => {
  // Seven peers is enough to say where you stand, not enough to publish
  // quartiles — the median of seven values is the fourth hospital's number.
  const out = B.buildPeerBenchmark({
    subjectValue: 0.4,
    peerValues: [0.21, 0.27, 0.33, 0.38, 0.44, 0.55, 0.68],
  });

  assert.equal(out.disclosure, B.DISCLOSURE.RANK_ONLY);
  assert.equal(out.percentileRank, 57); // four of seven peers below
  assert.equal(out.band, null);
  assert.match(out.note, /too few to publish a range/);
});

test("a uniform cohort withholds the band", () => {
  // Publishing the median here would publish all twelve hospitals' values.
  const out = B.buildPeerBenchmark({ subjectValue: 0.5, peerValues: many(12, () => 0.4) });

  assert.equal(out.disclosure, B.DISCLOSURE.RANK_ONLY);
  assert.equal(out.band, null);
  assert.match(out.note, /same value/);
});

test("a hospital with no value of its own is told so", () => {
  const out = B.buildPeerBenchmark({ subjectValue: null, peerValues: COHORT_12 });
  assert.equal(out.disclosure, B.DISCLOSURE.NONE);
  assert.equal(out.yourValue, null);
  assert.match(out.note, /No value reported/);
});

test("the floors are configurable for a data set that warrants it", () => {
  const out = B.buildPeerBenchmark({
    subjectValue: 0.4,
    peerValues: [0.2, 0.3, 0.5, 0.6, 0.7, 0.8],
    minCohort: 3,
    minCohortForQuartiles: 5,
  });
  assert.equal(out.disclosure, B.DISCLOSURE.BAND);
});

// --- the negative property: no peer's number ever escapes ------------------

function everyNumberIn(value, found = []) {
  if (typeof value === "number") found.push(value);
  else if (value && typeof value === "object") {
    for (const v of Object.values(value)) everyNumberIn(v, found);
  }
  return found;
}

test("the response never contains a minimum or a maximum", () => {
  // Min and max are always some specific hospital's figure, verbatim.
  const out = B.buildPeerBenchmark({ subjectValue: 0.31, peerValues: COHORT_12 });
  const keys = JSON.stringify(out);
  assert.equal(/"min"|"max"|"minimum"|"maximum"/.test(keys), false);
  assert.equal(everyNumberIn(out).includes(0.21), false, "lowest peer must not appear");
  assert.equal(everyNumberIn(out).includes(0.68), false, "highest peer must not appear");
});

test("the response never contains the peer list", () => {
  const out = B.buildPeerBenchmark({ subjectValue: 0.31, peerValues: COHORT_12 });
  const arrays = Object.values(out).filter(Array.isArray);
  assert.deepEqual(arrays, []);
});

test("no published figure is ever the cohort's extreme, across many cohorts", () => {
  // The extremes are the identifiable hospitals — the outlier is the one a
  // reader could guess at. Above the quartile floor the interpolation
  // positions sit well inside the cohort, so neither end can be published.
  //
  // Note this is deliberately not "no published value equals any peer's
  // value". A rounded median can coincide with some hospital's figure by
  // chance, and no amount of rounding prevents that. It is also not a
  // disclosure: nobody can tell the coincidence happened, or whose figure it
  // matched. What must never happen is publishing a value that is *knowably*
  // one hospital's — which is exactly what a minimum or maximum is.
  let leaks = 0;
  for (let trial = 0; trial < 500; trial += 1) {
    const peers = many(12 + (trial % 20), () => Math.round(Math.random() * 10000) / 10000);
    const out = B.buildPeerBenchmark({ subjectValue: 0.5, peerValues: peers });
    if (out.disclosure !== B.DISCLOSURE.BAND) continue;
    const low = Math.min(...peers);
    const high = Math.max(...peers);
    for (const published of [out.band.p25, out.band.p50, out.band.p75]) {
      if (published <= low || published >= high) leaks += 1;
    }
  }
  assert.equal(leaks, 0);
});

test("published values are rounded to the configured precision", () => {
  const out = B.buildPeerBenchmark({
    subjectValue: 0.123456,
    peerValues: many(15, (i) => 0.1 + i * 0.0123456),
  });
  assert.equal(out.yourValue, 0.12);
  for (const v of [out.band.p25, out.band.p50, out.band.p75]) {
    assert.equal(v, Math.round(v * 100) / 100);
  }
});

// --- differencing ---------------------------------------------------------

test("cohorts must come from the predefined set", () => {
  // Arbitrary client filters are what make one hospital subtractable from
  // two overlapping slices.
  assert.equal(B.assertPredefinedCohort(B.COHORTS.STATE_PEER_TYPE), "state_peer_type");
  assert.throws(
    () => B.assertPredefinedCohort("state=KS&beds<25"),
    /cohorts must come from the predefined set/,
  );
});

test("an unknown cohort is a client error, not a server error", () => {
  try {
    B.assertPredefinedCohort("nope");
    assert.fail("should have thrown");
  } catch (err) {
    assert.equal(err.status, 400);
    assert.equal(err.code, "unknown_cohort");
  }
});

// --- direction ------------------------------------------------------------

test("the response says which direction is good, so the UI need not guess", () => {
  const ccr = B.buildPeerBenchmark({ subjectValue: 0.31, peerValues: COHORT_12 });
  assert.equal(ccr.higherIsBetter, false);

  const margin = B.buildPeerBenchmark({
    subjectValue: 0.31,
    peerValues: COHORT_12,
    higherIsBetter: true,
  });
  assert.equal(margin.higherIsBetter, true);
});
