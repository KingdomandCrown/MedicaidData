/**
 * Which hospitals an organization may look at.
 *
 * `access-control.js` answers "who is this and what org are they in".
 * That is not yet enough for the scorecard app, because every route it
 * serves takes the hospital as a query parameter:
 *
 *     GET /api/scorecard?ccn=170027
 *
 * A verified, provisioned, correctly-roled user at Pratt Regional can change
 * six digits and read their competitor's margin, quality scores, 340B position,
 * and negotiated prices. Authentication does not stop that. Neither does a
 * role, because the role is right — they are allowed to read *a* scorecard.
 * The missing check is which one.
 *
 * So the org gets a roster of CCNs, and every route that accepts a CCN checks
 * against it. The roster lives in `orgs.json` next to `directory.json`: one
 * file says who works for whom, the other says what each customer bought.
 *
 * ## Two kinds of route, two different answers
 *
 * Routes that take a CCN (`/api/scorecard`, `/api/hospital`, `/api/decisions`)
 * are simple: assert the CCN is in the roster, or 403.
 *
 * Routes that read *across* hospitals are the interesting ones, and refusing
 * them outright would remove the product. `/api/state-ranking` scores every
 * hospital in a state by financial resilience and returns them by name; that
 * is a competitive-intelligence file on 120 Kansas hospitals, and no customer
 * bought it. But "you rank 31st of 120" is a fact about the subscriber, and
 * that is the thing worth paying for.
 *
 * `summarize()` draws that line: the caller's own rows come back whole, the
 * peers come back as a percentile rank and — only when the cohort is large
 * enough to hide an individual inside it — a quartile band. No peer names, no
 * peer values, no minimum, no maximum. The disclosure rules are `benchmark.js`'s;
 * this module supplies the partition they operate on.
 *
 * ## Fail closed
 *
 * An org with no entry in `orgs.json` sees nothing, rather than everything.
 * That is the direction the mistake has to point: a missing line in a config
 * file is a support ticket, and the other way round it is a breach.
 */

"use strict";

const { AccessError, ROLES } = require("./access-control");
const { buildPeerBenchmark } = require("./benchmark");

/** CMS certification numbers are six characters: two-digit state, four more. */
const CCN_RE = /^[0-9]{2}[0-9A-Z]{4}$/;

function normalizeCcn(value) {
  return String(value == null ? "" : value).trim().toUpperCase();
}

function denied(code, message) {
  return new AccessError(403, code, message);
}

/**
 * Turn `{orgId: {name, ccns}}` into a lookup.
 *
 * Rejected at build time, not request time: an unknown shape, a malformed CCN,
 * or the same CCN sold to two organizations. The last one matters most — it is
 * the config mistake that silently hands one customer another's hospital, and
 * it is invisible until someone reads a scorecard they should not have.
 *
 * Keys beginning with `_` are treated as comments, matching `directory.json`.
 */
function orgsFrom(mapping) {
  const table = new Map();
  const owner = new Map(); // ccn -> orgId, to catch a CCN sold twice

  for (const [orgId, entry] of Object.entries(mapping || {})) {
    if (orgId.startsWith("_")) continue;
    if (orgId === "*") {
      throw new Error('orgs.json must not define "*" — it is the cross-org role, not a customer');
    }
    if (!entry || typeof entry !== "object") {
      throw new Error(`orgs entry for ${orgId} must be an object`);
    }
    if (!Array.isArray(entry.ccns)) {
      throw new Error(`orgs entry for ${orgId} needs a ccns array`);
    }

    const ccns = new Set();
    for (const raw of entry.ccns) {
      // A CCN written as a JSON number, not a string, is the config mistake
      // that costs a Connecticut hospital its leading zero: 070027 parses as
      // 70027, matches nothing, and the customer sees an empty picker with no
      // error anywhere. Reject the type rather than coerce it.
      if (typeof raw !== "string") {
        throw new Error(
          `orgs entry for ${orgId} has a non-string CCN (${JSON.stringify(raw)}) — ` +
          "quote it, or a leading zero is lost",
        );
      }
      const ccn = normalizeCcn(raw);
      if (!CCN_RE.test(ccn)) {
        throw new Error(`orgs entry for ${orgId} has a malformed CCN: ${JSON.stringify(raw)}`);
      }
      const already = owner.get(ccn);
      if (already && already !== orgId) {
        throw new Error(`CCN ${ccn} is listed under both ${already} and ${orgId}`);
      }
      owner.set(ccn, orgId);
      ccns.add(ccn);
    }

    table.set(orgId, { orgId, name: entry.name || orgId, ccns });
  }

  return table;
}

/** The verified identity, or a denial. Never a default. */
function identityOf(req) {
  const identity = req && req.minerva;
  if (!identity) {
    throw new AccessError(401, "no_identity", "accessGuard must run before any scope check");
  }
  return identity;
}

/** True for the one role allowed to read across organizations. */
function isUnrestricted(identity) {
  return identity.role === ROLES.MINERVA_ADMIN && identity.org === "*";
}

/**
 * Find the row array inside a vault response.
 *
 * `/api/peers` and `/api/state-ranking` wrap their rows in objects whose shape
 * this module does not own. Rather than hard-code a key that a vault release
 * could rename — and fail *open* when it does — find the first array of objects
 * carrying a CCN. If there is no such array there is nothing to redact, and
 * `summarize` says so instead of guessing.
 */
function rowsOf(payload, ccnKey = "ccn") {
  if (Array.isArray(payload)) return { key: null, rows: payload };
  if (!payload || typeof payload !== "object") return { key: null, rows: null };
  for (const [key, value] of Object.entries(payload)) {
    if (!Array.isArray(value) || value.length === 0) continue;
    const first = value[0];
    if (first && typeof first === "object" && ccnKey in first) return { key, rows: value };
  }
  return { key: null, rows: null };
}

class Scope {
  constructor(mapping) {
    this._orgs = orgsFrom(mapping);
  }

  /** Organizations configured, for startup logging. */
  get size() {
    return this._orgs.size;
  }

  /**
   * The CCNs this caller may read, or `null` meaning "no restriction".
   *
   * `null` is returned only for a minerva_admin holding org `"*"`. An empty set
   * — an org with no hospitals, or no entry at all — is a real answer meaning
   * "nothing", and callers must not confuse the two.
   */
  ccnsFor(req) {
    const identity = identityOf(req);
    if (isUnrestricted(identity)) return null;
    const org = this._orgs.get(identity.org);
    return org ? org.ccns : new Set();
  }

  /** The organization's display name, for a header or an audit line. */
  labelFor(req) {
    const identity = identityOf(req);
    if (isUnrestricted(identity)) return "MinervaAI";
    const org = this._orgs.get(identity.org);
    return org ? org.name : identity.org;
  }

  allows(req, ccn) {
    const allowed = this.ccnsFor(req);
    if (allowed === null) return true;
    return allowed.has(normalizeCcn(ccn));
  }

  /**
   * Guard a route that takes a CCN.
   *
   * The denial says the same thing whether the CCN belongs to someone else or
   * does not exist. "Not yours" and "no such hospital" are different facts, and
   * telling them apart turns this endpoint into a directory of who is a
   * customer.
   */
  assertCcn(req, ccn) {
    const wanted = normalizeCcn(ccn);
    if (!wanted) throw new AccessError(400, "ccn_required", "a ccn is required");
    if (!this.allows(req, wanted)) {
      throw denied("ccn_out_of_scope", "that hospital is not in your organization");
    }
    return wanted;
  }

  /** Keep only the rows this caller may see. Used for the hospital picker. */
  filter(req, rows, ccnKey = "ccn") {
    const allowed = this.ccnsFor(req);
    if (allowed === null) return rows || [];
    return (rows || []).filter((row) => row && allowed.has(normalizeCcn(row[ccnKey])));
  }

  /** Split rows into the caller's own and everyone else's. */
  partition(req, rows, ccnKey = "ccn") {
    const allowed = this.ccnsFor(req);
    const mine = [];
    const others = [];
    for (const row of rows || []) {
      if (!row) continue;
      if (allowed === null || allowed.has(normalizeCcn(row[ccnKey]))) mine.push(row);
      else others.push(row);
    }
    return { mine, others };
  }

  /**
   * A cross-hospital result the subscriber may keep: their own rows in full,
   * everyone else's as a position.
   *
   * Returns the caller's rows unchanged for an unrestricted caller, so the
   * internal view of `/api/state-ranking` is not degraded by a control meant
   * for customers.
   *
   * `valueKey` names the number being ranked. Each of the caller's rows gets
   * its own comparison, because an org may hold more than one hospital and
   * "you rank 31st" means nothing across two of them.
   */
  summarize(req, payload, options = {}) {
    const {
      valueKey = "score",
      ccnKey = "ccn",
      rankKey = "rank",
      cohortLabel = "peers",
      higherIsBetter = true,
      precision,
    } = options;

    const identity = identityOf(req);
    const { rows } = rowsOf(payload, ccnKey);

    if (!rows) {
      // Nothing recognizable to partition. Returning the payload here would be
      // failing open on a shape we did not understand.
      return {
        restricted: !isUnrestricted(identity),
        cohortSize: 0,
        yours: [],
        note: "No comparable rows in this result.",
      };
    }

    if (isUnrestricted(identity)) {
      return Object.assign({ restricted: false }, payload);
    }

    const { mine, others } = this.partition(req, rows, ccnKey);
    const peerValues = others
      .map((row) => Number(row[valueKey]))
      .filter((v) => Number.isFinite(v));

    const yours = mine.map((row) => {
      const benchmark = buildPeerBenchmark({
        subjectValue: Number.isFinite(Number(row[valueKey])) ? Number(row[valueKey]) : null,
        peerValues,
        cohortLabel,
        higherIsBetter,
        precision,
      });
      return Object.assign({}, row, {
        // `rank` is "31st of 120" — a fact about the subject, and the number a
        // CFO actually asks for. It names no peer.
        rankOf: rows.length,
        percentileRank: benchmark.percentileRank,
        band: benchmark.band,
        disclosure: benchmark.disclosure,
        note: benchmark.note,
        [rankKey]: row[rankKey],
      });
    });

    return {
      restricted: true,
      cohortLabel,
      cohortSize: rows.length,
      peerCount: peerValues.length,
      yours,
    };
  }
}

function createScope(mapping) {
  return new Scope(mapping);
}

module.exports = {
  Scope,
  createScope,
  orgsFrom,
  rowsOf,
  normalizeCcn,
  CCN_RE,
};
