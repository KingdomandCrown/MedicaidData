/**
 * MinervaAI — AI Readiness · Agent 1: Readiness Scorer
 *
 * DETERMINISTIC BY DESIGN. No model call lives in this path: scoring must be
 * reproducible and auditable, and a language model here would be a liability
 * (build spec §9, Agent 1). Pure function — responses in, scores + placement +
 * gate flags out.
 *
 * All rules, weights, thresholds, and item metadata come from
 * minerva_readiness_content.json. Nothing about the instrument is hardcoded
 * here except the arithmetic the spec defines.
 *
 * Usage:
 *   const { score } = require("./scoring");
 *   const result = score(responses, content, { iciThreshold, oeiThreshold });
 *
 * `responses` may be either:
 *   [{ item_id: "D3.1", value: 4, na: false }, ...]        (spec §10 shape)
 *   { "D3.1": 4, "D3.2": { value: 3, na: false }, ... }    (map shorthand)
 * Scenario items take the option key ("B") or the 1-5 value it maps to.
 */
"use strict";

const LIKERT_MIN = 1;
const LIKERT_MAX = 5;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function indexContent(content) {
  const items = {};
  content.items.forEach((it) => { items[it.id] = it; });
  const dims = {};
  content.dimensions.forEach((d) => { dims[d.id] = d; });
  return { items, dims };
}

function normalizeResponses(responses) {
  const out = {};
  if (Array.isArray(responses)) {
    responses.forEach((r) => {
      if (!r || !r.item_id) return;
      out[r.item_id] = { value: r.value, na: !!r.na };
    });
  } else if (responses && typeof responses === "object") {
    Object.keys(responses).forEach((k) => {
      const v = responses[k];
      out[k] = (v && typeof v === "object" && !Array.isArray(v))
        ? { value: v.value, na: !!v.na }
        : { value: v, na: false };
    });
  }
  return out;
}

function mean(nums) {
  if (!nums.length) return null;
  return nums.reduce((a, b) => a + b, 0) / nums.length;
}

/** 0-100 normalization of a 1-5 raw score: ((raw - 1) / 4) * 100  (spec §5.2) */
function normalize(raw) {
  return ((raw - LIKERT_MIN) / (LIKERT_MAX - LIKERT_MIN)) * 100;
}

// ---------------------------------------------------------------------------
// item scoring (spec §5.1)
// ---------------------------------------------------------------------------

/**
 * Score a single item to the 1-5 scale.
 * Returns null when the item is N/A or unanswered (excluded from means,
 * never scored zero).
 */
function scoreItem(item, response, content) {
  if (!item) return null;
  if (!response || response.na === true) return null;

  const raw = response.value;
  if (raw === null || raw === undefined || raw === "") return null;

  if (item.type === "scenario") {
    const correctValue = content.scoring.scenario_correct_value;    // 5
    const incorrectValue = content.scoring.scenario_incorrect_value; // 1
    // Accept an option key ("B") or a pre-scored numeric value.
    if (typeof raw === "string") {
      const opt = (item.options || []).find(
        (o) => String(o.key).toUpperCase() === raw.trim().toUpperCase()
      );
      if (!opt) return null;
      return opt.correct ? correctValue : incorrectValue;
    }
    const n = Number(raw);
    if (!isFinite(n)) return null;
    // Binary, no partial credit.
    return n >= correctValue ? correctValue : incorrectValue;
  }

  if (item.type === "ladder") {
    const n = Number(raw);
    if (!isFinite(n)) return null;
    // Rescale 1-4 onto 1-5: 1 + (raw - 1) * (4/3)   (spec §5.1)
    return 1 + (n - 1) * (4 / 3);
  }

  // likert
  const n = Number(raw);
  if (!isFinite(n)) return null;
  return Math.min(LIKERT_MAX, Math.max(LIKERT_MIN, n));
}

// ---------------------------------------------------------------------------
// dimension scoring (spec §5.2)
// ---------------------------------------------------------------------------

function scoreDimension(dimId, content, idx, resp) {
  const dimItems = content.items.filter((it) => it.dimension === dimId);
  const scored = [];   // { id, value }
  let naCount = 0;

  dimItems.forEach((it) => {
    const v = scoreItem(it, resp[it.id], content);
    if (v === null) { naCount += 1; return; }
    scored.push({ id: it.id, value: v });
  });

  // "If more than half of a dimension's items are marked N/A, mark the
  //  dimension insufficient and exclude it from targeting." (spec §5.1)
  const insufficient = dimItems.length > 0 && naCount > dimItems.length / 2;

  if (!scored.length) {
    return { id: dimId, raw: null, score: null, insufficient: true, answered: 0, total: dimItems.length };
  }

  // Objective blend for D3 / D4: 0.70 * self-report + 0.30 * objective (spec §5.2)
  const blend = (content.scoring.objective_blend || {})[dimId];
  let raw;
  if (blend) {
    const objIds = blend.objective_items || [];
    const objective = scored.filter((s) => objIds.indexOf(s.id) >= 0).map((s) => s.value);
    const selfReport = scored.filter((s) => objIds.indexOf(s.id) < 0).map((s) => s.value);
    const mSelf = mean(selfReport);
    const mObj = mean(objective);
    if (mSelf !== null && mObj !== null) {
      raw = blend.self_report_weight * mSelf + blend.objective_weight * mObj;
    } else {
      // One component entirely N/A — fall back to whichever exists, so the
      // blend can never silently zero out a dimension.
      raw = mSelf !== null ? mSelf : mObj;
    }
  } else {
    raw = mean(scored.map((s) => s.value));
  }

  return {
    id: dimId,
    raw,
    score: Math.round(normalize(raw)),
    insufficient,
    answered: scored.length,
    total: dimItems.length,
  };
}

// ---------------------------------------------------------------------------
// axes, placement, gates (spec §5.3 - §5.5)
// ---------------------------------------------------------------------------

/** ICI = 0.15*D1 + 0.25*D2 + 0.25*D3 + 0.20*D4 + 0.15*D5, weights from content. */
function computeIci(dimScores, content) {
  const individual = content.dimensions.filter((d) => d.axis === "individual");
  let weighted = 0;
  let weightUsed = 0;
  individual.forEach((d) => {
    const s = dimScores[d.id];
    if (!s || s.score === null) return;
    weighted += d.weight * s.score;
    weightUsed += d.weight;
  });
  if (!weightUsed) return null;
  // Re-normalize by the weight actually available so an insufficient
  // dimension doesn't drag the index toward zero.
  return Math.round(weighted / weightUsed);
}

function computeOei(dimScores, content) {
  const org = content.dimensions.filter((d) => d.axis === "organizational");
  if (!org.length) return null;
  const s = dimScores[org[0].id];
  return s && s.score !== null ? s.score : null;
}

function placementFor(ici, oei, iciThreshold, oeiThreshold, content) {
  const m = content.scoring.placement_matrix;
  const highIci = ici !== null && ici >= iciThreshold;
  const highOei = oei !== null && oei >= oeiThreshold;
  if (highIci && highOei) return m.high_ici_high_oei;   // frontier
  if (highIci && !highOei) return m.high_ici_low_oei;   // blocked_agency
  if (!highIci && highOei) return m.low_ici_high_oei;   // emergent
  return m.low_ici_low_oei;                             // early
}

/**
 * Governance gate (spec §5.5): if D4 < 50, placement cannot display as
 * frontier, D4 is forced as a target, and governance actions lead the plan.
 * Someone who delegates aggressively without risk judgment is not Frontier in
 * a regulated setting; they are exposed.
 */
function applyGovernanceGate(placement, dimScores, content) {
  const gate = ((content.scoring.gates || {}).governance_first) || null;
  if (!gate) return { placement, gates: {}, forcedTarget: null };

  const d4 = dimScores.D4;
  const tripped = !!d4 && d4.score !== null && d4.score < 50;
  if (!tripped) return { placement, gates: { governance_first: false }, forcedTarget: null };

  // cap_placement_at_emergent: frontier is not displayable under the gate.
  const capped = placement === "frontier" ? "emergent" : placement;
  return {
    placement: capped,
    gates: { governance_first: true },
    forcedTarget: "D4",
  };
}

// ---------------------------------------------------------------------------
// target selection (spec §7.1)
// ---------------------------------------------------------------------------

/**
 * 1. governance gate forces D4 first
 * 2. rank remaining individual dimensions by score ascending
 * 3. prerequisite ordering D4 -> D3 -> D2 -> D1 -> D5: a dimension cannot be
 *    targeted ahead of an unmet prerequisite (one ranked earlier scoring < 50)
 * 4. exclude insufficient dimensions
 * 5. take the first two   (hard cap — six targets is a curriculum)
 */
function selectTargets(dimScores, content, forcedTarget) {
  const individual = content.dimensions
    .filter((d) => d.axis === "individual")
    .slice()
    .sort((a, b) => (a.prerequisite_rank || 99) - (b.prerequisite_rank || 99));

  const eligible = individual.filter((d) => {
    const s = dimScores[d.id];
    return s && s.score !== null && !s.insufficient;
  });

  const targets = [];
  if (forcedTarget && eligible.some((d) => d.id === forcedTarget)) targets.push(forcedTarget);

  // Unmet prerequisites (scoring below 50), in prerequisite order, come first.
  eligible.forEach((d) => {
    if (targets.length >= 2) return;
    if (targets.indexOf(d.id) >= 0) return;
    if (dimScores[d.id].score < 50) targets.push(d.id);
  });

  // Then fill by lowest score.
  eligible
    .slice()
    .sort((a, b) => dimScores[a.id].score - dimScores[b.id].score)
    .forEach((d) => {
      if (targets.length >= 2) return;
      if (targets.indexOf(d.id) >= 0) return;
      targets.push(d.id);
    });

  return targets.slice(0, 2);
}

// ---------------------------------------------------------------------------
// public API
// ---------------------------------------------------------------------------

/**
 * Score a completed assessment.
 *
 * @param {Array|Object} responses  see module docblock
 * @param {Object} content          minerva_readiness_content.json
 * @param {Object} [options]        { iciThreshold, oeiThreshold }
 *        Thresholds default to the content file's values. Per spec §5.6 the
 *        defaults are provisional — replace with pilot medians before real use.
 * @returns {Object} scores object (spec §10 `scores` shape, plus detail)
 */
function score(responses, content, options) {
  if (!content || !content.items || !content.dimensions) {
    throw new Error("scoring: content bank is missing items/dimensions");
  }
  const opts = options || {};
  const th = (content.scoring && content.scoring.thresholds) || {};
  const iciThreshold = opts.iciThreshold !== undefined ? opts.iciThreshold : th.ici_threshold;
  const oeiThreshold = opts.oeiThreshold !== undefined ? opts.oeiThreshold : th.oei_threshold;

  const idx = indexContent(content);
  const resp = normalizeResponses(responses);

  const detail = {};
  const dimensions = {};
  const insufficient = [];
  content.dimensions.forEach((d) => {
    const s = scoreDimension(d.id, content, idx, resp);
    detail[d.id] = s;
    dimensions[d.id] = s.score;
    if (s.insufficient) insufficient.push(d.id);
  });

  const ici = computeIci(detail, content);
  const oei = computeOei(detail, content);

  const rawPlacement = placementFor(ici, oei, iciThreshold, oeiThreshold, content);
  const gated = applyGovernanceGate(rawPlacement, detail, content);
  const targets = selectTargets(detail, content, gated.forcedTarget);

  return {
    dimensions,
    detail,
    ici,
    oei,
    placement: gated.placement,
    placement_ungated: rawPlacement,
    gates: gated.gates,
    targets,
    insufficient,
    thresholds: { ici: iciThreshold, oei: oeiThreshold, provisional: true },
  };
}

module.exports = {
  score,
  // exported for unit tests / reuse
  scoreItem,
  scoreDimension,
  computeIci,
  computeOei,
  placementFor,
  applyGovernanceGate,
  selectTargets,
  normalize,
};
