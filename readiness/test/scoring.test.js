/**
 * Unit tests for the deterministic Readiness Scorer.
 * Run: node --test readiness/test/   (or: node readiness/test/scoring.test.js)
 *
 * Covers every branch the build spec defines: item scoring (likert / ladder
 * rescale / scenario binary), N/A exclusion and the insufficient rule, the
 * D3+D4 objective blend, ICI/OEI, all four placements, the governance gate,
 * target selection with prerequisite ordering, and determinism.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");

const content = require(path.join(__dirname, "..", "minerva_readiness_content.json"));
const S = require(path.join(__dirname, "..", "scoring.js"));

// --- helpers --------------------------------------------------------------

const ITEMS = content.items;
const byId = {};
ITEMS.forEach((i) => { byId[i.id] = i; });

/** Build a full response set: every likert = `v`, ladder = `ladder`, scenarios correct/incorrect. */
function buildResponses(opts) {
  const o = Object.assign({ likert: 3, ladder: 2, scenariosCorrect: true, overrides: {} }, opts || {});
  const r = {};
  ITEMS.forEach((it) => {
    if (it.type === "scenario") {
      const correct = (it.options || []).find((x) => x.correct);
      const wrong = (it.options || []).find((x) => !x.correct);
      r[it.id] = { value: (o.scenariosCorrect ? correct.key : wrong.key), na: false };
    } else if (it.type === "ladder") {
      r[it.id] = { value: o.ladder, na: false };
    } else {
      r[it.id] = { value: o.likert, na: false };
    }
  });
  Object.keys(o.overrides).forEach((k) => {
    const ov = o.overrides[k];
    r[k] = (ov && typeof ov === "object") ? ov : { value: ov, na: false };
  });
  return r;
}

/** Set every item of a dimension to a likert value. */
function setDim(r, dim, value) {
  ITEMS.filter((i) => i.dimension === dim).forEach((i) => {
    if (i.type === "scenario") {
      const opt = (i.options || []).find((x) => (value >= 5 ? x.correct : !x.correct));
      r[i.id] = { value: opt.key, na: false };
    } else if (i.type === "ladder") {
      r[i.id] = { value: value >= 5 ? 4 : 1, na: false };
    } else {
      r[i.id] = { value, na: false };
    }
  });
  return r;
}

// --- content bank sanity --------------------------------------------------

test("content bank matches the spec's shape", () => {
  assert.strictEqual(ITEMS.length, 28, "28 items");
  assert.strictEqual(content.dimensions.length, 6, "6 dimensions");
  const individual = content.dimensions.filter((d) => d.axis === "individual");
  assert.strictEqual(individual.length, 5);
  const wsum = individual.reduce((a, d) => a + d.weight, 0);
  assert.ok(Math.abs(wsum - 1) < 1e-9, "individual weights sum to 1");
  assert.strictEqual(ITEMS.filter((i) => i.type === "scenario").length, 4);
});

// --- item scoring ---------------------------------------------------------

test("likert items score 1-5 and clamp", () => {
  const it = byId["D1.1"];
  assert.strictEqual(S.scoreItem(it, { value: 4 }, content), 4);
  assert.strictEqual(S.scoreItem(it, { value: 9 }, content), 5);
  assert.strictEqual(S.scoreItem(it, { value: 0 }, content), 1);
});

test("ladder rescales 1-4 onto 1-5 per 1 + (raw-1)*(4/3)", () => {
  const it = byId["D2.1"];
  assert.strictEqual(S.scoreItem(it, { value: 1 }, content), 1);
  assert.strictEqual(S.scoreItem(it, { value: 4 }, content), 5);
  assert.ok(Math.abs(S.scoreItem(it, { value: 2 }, content) - (1 + 4 / 3)) < 1e-9);
  assert.ok(Math.abs(S.scoreItem(it, { value: 3 }, content) - (1 + 8 / 3)) < 1e-9);
});

test("scenario items are binary: correct=5, incorrect=1, no partial credit", () => {
  const it = byId["D3.4"];
  const correct = it.options.find((o) => o.correct).key;   // B
  const wrong = it.options.find((o) => !o.correct).key;
  assert.strictEqual(S.scoreItem(it, { value: correct }, content), 5);
  assert.strictEqual(S.scoreItem(it, { value: wrong }, content), 1);
  assert.strictEqual(S.scoreItem(it, { value: "b" }, content), 5, "case-insensitive key");
});

test("N/A and unanswered are excluded, never scored zero", () => {
  const it = byId["D1.1"];
  assert.strictEqual(S.scoreItem(it, { value: 4, na: true }, content), null);
  assert.strictEqual(S.scoreItem(it, { value: null }, content), null);
  assert.strictEqual(S.scoreItem(it, undefined, content), null);
});

// --- dimension scoring ----------------------------------------------------

test("dimension normalizes to 0-100: all 3s -> 50, all 5s -> 100, all 1s -> 0", () => {
  let r = buildResponses({ likert: 3 });
  assert.strictEqual(S.score(r, content).dimensions.D1, 50);
  r = setDim(buildResponses({}), "D1", 5);
  assert.strictEqual(S.score(r, content).dimensions.D1, 100);
  r = setDim(buildResponses({}), "D1", 1);
  assert.strictEqual(S.score(r, content).dimensions.D1, 0);
});

test("N/A items are excluded from the dimension mean (not zeroed)", () => {
  // D1: three 5s + one N/A  -> mean of the three = 5 -> 100 (not 75)
  const r = buildResponses({ likert: 5 });
  r["D1.1"] = { value: 5, na: true };
  const res = S.score(r, content);
  assert.strictEqual(res.dimensions.D1, 100);
  assert.strictEqual(res.detail.D1.answered, 3);
});

test("more than half a dimension N/A marks it insufficient", () => {
  const r = buildResponses({ likert: 4 });
  // D1 has 4 items; mark 3 N/A (>50%)
  ["D1.1", "D1.2", "D1.3"].forEach((id) => { r[id] = { value: 4, na: true }; });
  const res = S.score(r, content);
  assert.strictEqual(res.detail.D1.insufficient, true);
  assert.ok(res.insufficient.indexOf("D1") >= 0);
  assert.ok(res.targets.indexOf("D1") < 0, "insufficient dimensions are excluded from targeting");
});

test("D3/D4 objective blend applies 0.70 self-report + 0.30 objective", () => {
  const r = buildResponses({ likert: 3 });
  // D3: self-report items all 5, both scenarios wrong (=1)
  ["D3.1", "D3.2", "D3.3"].forEach((id) => { r[id] = { value: 5, na: false }; });
  ["D3.4", "D3.5"].forEach((id) => {
    const wrong = byId[id].options.find((o) => !o.correct).key;
    r[id] = { value: wrong, na: false };
  });
  const expectedRaw = 0.7 * 5 + 0.3 * 1;              // 3.8
  const expected = Math.round(((expectedRaw - 1) / 4) * 100); // 70
  assert.strictEqual(S.score(r, content).dimensions.D3, expected);
});

// --- axes -----------------------------------------------------------------

test("ICI is the weighted sum of D1..D5; OEI is D6", () => {
  const r = buildResponses({ likert: 3, ladder: 2, scenariosCorrect: true });
  const res = S.score(r, content);
  const d = res.dimensions;
  const manual = Math.round(0.15 * d.D1 + 0.25 * d.D2 + 0.25 * d.D3 + 0.20 * d.D4 + 0.15 * d.D5);
  assert.strictEqual(res.ici, manual);
  assert.strictEqual(res.oei, d.D6);
});

// --- placement matrix -----------------------------------------------------

test("all four placements resolve correctly", () => {
  assert.strictEqual(S.placementFor(80, 80, 60, 60, content), "frontier");
  assert.strictEqual(S.placementFor(80, 40, 60, 60, content), "blocked_agency");
  assert.strictEqual(S.placementFor(40, 80, 60, 60, content), "emergent");
  assert.strictEqual(S.placementFor(40, 40, 60, 60, content), "early");
});

test("threshold is inclusive at the boundary", () => {
  assert.strictEqual(S.placementFor(60, 60, 60, 60, content), "frontier");
  assert.strictEqual(S.placementFor(59, 60, 60, 60, content), "emergent");
});

test("high scores across the board land in frontier", () => {
  const r = buildResponses({ likert: 5, ladder: 4, scenariosCorrect: true });
  const res = S.score(r, content);
  assert.strictEqual(res.placement, "frontier");
  assert.strictEqual(res.gates.governance_first, false);
});

test("capable individual + weak organization = blocked_agency", () => {
  let r = buildResponses({ likert: 5, ladder: 4, scenariosCorrect: true });
  r = setDim(r, "D6", 1);
  const res = S.score(r, content);
  assert.ok(res.ici >= 60 && res.oei < 60);
  assert.strictEqual(res.placement, "blocked_agency");
});

// --- governance gate ------------------------------------------------------

test("governance gate: D4 < 50 caps frontier at emergent and forces D4 first", () => {
  let r = buildResponses({ likert: 5, ladder: 4, scenariosCorrect: true });
  r = setDim(r, "D4", 1);            // D4 -> 0, well under 50
  const res = S.score(r, content);
  assert.strictEqual(res.detail.D4.score < 50, true);
  assert.strictEqual(res.gates.governance_first, true);
  assert.strictEqual(res.placement_ungated, "frontier");
  assert.strictEqual(res.placement, "emergent", "frontier is not displayable under the gate");
  assert.strictEqual(res.targets[0], "D4", "D4 is forced as the first target");
});

test("governance gate does not trip at D4 >= 50", () => {
  const r = buildResponses({ likert: 3, ladder: 2, scenariosCorrect: true });
  const res = S.score(r, content);
  assert.ok(res.detail.D4.score >= 50);
  assert.strictEqual(res.gates.governance_first, false);
});

// --- target selection -----------------------------------------------------

test("hard cap of two targeted dimensions", () => {
  const r = buildResponses({ likert: 1, ladder: 1, scenariosCorrect: false });
  const res = S.score(r, content);
  assert.strictEqual(res.targets.length, 2);
});

test("prerequisite ordering puts D4 before D3 when both are unmet", () => {
  let r = buildResponses({ likert: 5, ladder: 4, scenariosCorrect: true });
  r = setDim(r, "D3", 1);
  r = setDim(r, "D4", 1);
  const res = S.score(r, content);
  assert.deepStrictEqual(res.targets, ["D4", "D3"]);
});

test("with no unmet prerequisites, the two lowest dimensions are targeted", () => {
  let r = buildResponses({ likert: 5, ladder: 4, scenariosCorrect: true });
  r = setDim(r, "D1", 4);   // 75
  r = setDim(r, "D5", 3);   // 50
  const res = S.score(r, content);
  assert.ok(res.targets.indexOf("D5") >= 0, "lowest is targeted");
  assert.strictEqual(res.targets.length, 2);
  assert.strictEqual(res.gates.governance_first, false);
});

test("targets never include the organizational dimension D6", () => {
  let r = buildResponses({ likert: 5, ladder: 4, scenariosCorrect: true });
  r = setDim(r, "D6", 1);
  const res = S.score(r, content);
  assert.ok(res.targets.indexOf("D6") < 0);
});

// --- response shapes & determinism ---------------------------------------

test("accepts both the array response shape and the map shorthand", () => {
  const map = buildResponses({ likert: 4, ladder: 3, scenariosCorrect: true });
  const arr = Object.keys(map).map((k) => ({ item_id: k, value: map[k].value, na: map[k].na }));
  assert.deepStrictEqual(S.score(arr, content).dimensions, S.score(map, content).dimensions);
});

test("scoring is deterministic and side-effect free", () => {
  const r = buildResponses({ likert: 4, ladder: 3, scenariosCorrect: true });
  const snapshot = JSON.stringify(r);
  const a = S.score(r, content);
  const b = S.score(r, content);
  assert.deepStrictEqual(a, b);
  assert.strictEqual(JSON.stringify(r), snapshot, "input is not mutated");
});

test("thresholds are overridable and flagged provisional (pilot calibration)", () => {
  const r = buildResponses({ likert: 3, ladder: 2, scenariosCorrect: true });
  const strict = S.score(r, content, { iciThreshold: 95, oeiThreshold: 95 });
  assert.strictEqual(strict.placement, "early");
  assert.strictEqual(strict.thresholds.provisional, true);
});

test("an empty submission does not crash and yields no placement data", () => {
  const res = S.score({}, content);
  assert.strictEqual(res.ici, null);
  assert.strictEqual(res.oei, null);
  assert.strictEqual(res.insufficient.length, 6);
  assert.strictEqual(res.targets.length, 0);
});
