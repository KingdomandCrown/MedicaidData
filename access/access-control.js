/**
 * MinervaAI tenant + role access control, on top of Cloudflare Access.
 *
 * Cloudflare Access already answers "who is this" — approved email plus MFA.
 * What it does not answer is "which hospital's data may this person see", which
 * is the question a pilot with a named hospital turns on. This module supplies
 * that second half: it verifies the identity Access asserts, maps the email to
 * an organization and a role, and gives the app a single place to scope every
 * query.
 *
 * Two things it deliberately does NOT do:
 *
 *   1. Trust the `Cf-Access-Authenticated-User-Email` header. Any client that
 *      can reach the origin directly can set that header. The signed JWT is
 *      verified against Cloudflare's public keys instead, so a request that
 *      did not come through Access is rejected even if it reaches the port.
 *
 *   2. Rely on a model to keep tenants apart. `orgFilter()` is meant to be
 *      called by the retrieval layer so an agent physically cannot read
 *      another org's rows. A system prompt asking the model not to reveal
 *      other hospitals is not a control.
 *
 * Every failure path denies. An unknown email, an expired token, a missing
 * audience, an unreachable key set — all produce a denial, never a default
 * identity.
 *
 * Zero dependencies: Node's built-in crypto verifies RS256 directly.
 */

"use strict";

const crypto = require("node:crypto");

// --- roles ----------------------------------------------------------------

/** Roles, narrowest first. */
const ROLES = Object.freeze({
  ORG_VIEWER: "org_viewer",   // read their own scorecard
  ORG_MEMBER: "org_member",   // + use the coaches/agents
  ORG_ADMIN: "org_admin",     // + manage their own org's users
  MINERVA_ADMIN: "minerva_admin", // crosses orgs; the only role that may
});

const _RANK = {
  [ROLES.ORG_VIEWER]: 0,
  [ROLES.ORG_MEMBER]: 1,
  [ROLES.ORG_ADMIN]: 2,
  [ROLES.MINERVA_ADMIN]: 3,
};

function isRole(role) {
  return Object.prototype.hasOwnProperty.call(_RANK, role);
}

/** True when `role` is at least as privileged as `minimum`. */
function roleAtLeast(role, minimum) {
  if (!isRole(role) || !isRole(minimum)) return false;
  return _RANK[role] >= _RANK[minimum];
}

// --- errors ---------------------------------------------------------------

class AccessError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "AccessError";
    this.status = status;
    this.code = code;
  }
}

const denied = (code, message) => new AccessError(403, code, message);
const unauthenticated = (code, message) => new AccessError(401, code, message);

// --- base64url ------------------------------------------------------------

function b64urlToBuffer(input) {
  const padded = input.replace(/-/g, "+").replace(/_/g, "/");
  return Buffer.from(padded, "base64");
}

function decodeJsonSegment(segment, what) {
  try {
    return JSON.parse(b64urlToBuffer(segment).toString("utf8"));
  } catch (err) {
    throw unauthenticated("malformed_token", `token ${what} is not valid JSON`);
  }
}

// --- JWKS -----------------------------------------------------------------

/**
 * Cloudflare's signing keys, cached.
 *
 * Keys rotate, so a `kid` we have never seen forces one refresh rather than a
 * denial — but only one, so an attacker cannot use unknown kids to hammer the
 * certs endpoint.
 */
class KeyCache {
  constructor({ teamDomain, fetchImpl, ttlMs = 10 * 60 * 1000, now = Date.now }) {
    this._url = `https://${teamDomain}/cdn-cgi/access/certs`;
    this._fetch = fetchImpl || globalThis.fetch;
    this._ttlMs = ttlMs;
    this._now = now;
    this._keys = new Map();
    this._fetchedAt = 0;
    this._inflight = null;
  }

  async _refresh() {
    // Collapse concurrent refreshes into one request.
    if (this._inflight) return this._inflight;
    this._inflight = (async () => {
      let payload;
      try {
        const res = await this._fetch(this._url);
        if (!res || !res.ok) {
          throw new Error(`certs endpoint returned ${res && res.status}`);
        }
        payload = await res.json();
      } catch (err) {
        throw unauthenticated("keys_unavailable", `could not load Access keys: ${err.message}`);
      } finally {
        this._inflight = null;
      }

      const keys = new Map();
      for (const jwk of (payload && payload.keys) || []) {
        if (!jwk.kid || jwk.kty !== "RSA") continue;
        try {
          keys.set(jwk.kid, crypto.createPublicKey({ key: jwk, format: "jwk" }));
        } catch {
          // A key we cannot parse is skipped, not fatal — the others still work.
        }
      }
      if (keys.size === 0) {
        throw unauthenticated("keys_unavailable", "Access certs endpoint returned no usable keys");
      }
      this._keys = keys;
      this._fetchedAt = this._now();
      return keys;
    })();
    return this._inflight;
  }

  async get(kid) {
    const stale = this._now() - this._fetchedAt > this._ttlMs;
    if (this._keys.size === 0 || stale) await this._refresh();
    if (!this._keys.has(kid)) await this._refresh(); // key rotation
    const key = this._keys.get(kid);
    if (!key) throw unauthenticated("unknown_key", "token signed by an unrecognized key");
    return key;
  }
}

// --- token verification ---------------------------------------------------

/**
 * Verify a Cloudflare Access JWT and return its payload.
 *
 * Checks signature, issuer, audience, and expiry. `aud` matters as much as the
 * signature: without it a token minted for a *different* Access application in
 * the same team would sail through.
 */
async function verifyAccessToken(token, { keys, teamDomain, aud, now = Date.now, clockSkewSec = 60 }) {
  if (typeof token !== "string" || token.split(".").length !== 3) {
    throw unauthenticated("malformed_token", "expected a three-part JWT");
  }
  const [headerB64, payloadB64, signatureB64] = token.split(".");

  const header = decodeJsonSegment(headerB64, "header");
  if (header.alg !== "RS256") {
    // Refusing anything else closes the "alg: none" family of attacks.
    throw unauthenticated("bad_algorithm", `unsupported token algorithm: ${header.alg}`);
  }
  if (!header.kid) throw unauthenticated("malformed_token", "token header has no kid");

  const key = await keys.get(header.kid);
  const signingInput = Buffer.from(`${headerB64}.${payloadB64}`, "utf8");
  const ok = crypto.verify(
    "RSA-SHA256",
    signingInput,
    key,
    b64urlToBuffer(signatureB64),
  );
  if (!ok) throw unauthenticated("bad_signature", "token signature did not verify");

  const payload = decodeJsonSegment(payloadB64, "payload");

  const expectedIssuer = `https://${teamDomain}`;
  if (payload.iss !== expectedIssuer) {
    throw unauthenticated("bad_issuer", `token issuer ${payload.iss} is not ${expectedIssuer}`);
  }

  const audiences = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
  if (!aud || !audiences.includes(aud)) {
    throw unauthenticated("bad_audience", "token was not issued for this application");
  }

  const nowSec = Math.floor(now() / 1000);
  if (typeof payload.exp !== "number" || payload.exp + clockSkewSec < nowSec) {
    throw unauthenticated("expired", "token has expired");
  }
  if (typeof payload.nbf === "number" && payload.nbf - clockSkewSec > nowSec) {
    throw unauthenticated("not_yet_valid", "token is not valid yet");
  }
  if (!payload.email) {
    throw unauthenticated("no_email", "token carries no email claim");
  }

  return payload;
}

// --- directory ------------------------------------------------------------

/**
 * Turn a plain `{email: {org, role}}` object into a lookup function.
 *
 * Emails are compared lowercased. An entry with an unknown role is rejected at
 * build time rather than silently granting whatever the app happens to check.
 */
function directoryFrom(mapping) {
  if (typeof mapping === "function") return mapping;
  const table = new Map();
  for (const [email, entry] of Object.entries(mapping || {})) {
    if (!entry || !entry.org || !entry.role) {
      throw new Error(`directory entry for ${email} needs both org and role`);
    }
    if (!isRole(entry.role)) {
      throw new Error(`directory entry for ${email} has unknown role: ${entry.role}`);
    }
    if (entry.role !== ROLES.MINERVA_ADMIN && entry.org === "*") {
      throw new Error(`only ${ROLES.MINERVA_ADMIN} may hold org "*" (${email})`);
    }
    table.set(email.trim().toLowerCase(), { org: entry.org, role: entry.role });
  }
  return (email) => table.get(String(email).trim().toLowerCase()) || null;
}

// --- middleware -----------------------------------------------------------

function readToken(req) {
  const header = req.headers && (req.headers["cf-access-jwt-assertion"] ||
    req.headers["Cf-Access-Jwt-Assertion"]);
  if (header) return String(header);
  // Access also sets a cookie, which is what a browser navigation carries.
  const cookie = (req.headers && req.headers.cookie) || "";
  const match = /(?:^|;\s*)CF_Authorization=([^;]+)/.exec(cookie);
  return match ? match[1] : null;
}

/**
 * Express middleware: verify the Access token, attach `req.minerva`.
 *
 * `req.minerva` is `{ email, org, role }`. Downstream code should read the org
 * from there and nowhere else — never from a query string or request body,
 * which the client controls.
 */
function createAccessGuard(options) {
  const {
    teamDomain,
    aud,
    directory,
    fetchImpl,
    now = Date.now,
    jwksTtlMs,
    onAudit,
  } = options || {};

  if (!teamDomain) throw new Error("createAccessGuard needs teamDomain");
  if (!aud) throw new Error("createAccessGuard needs aud (the Access application AUD tag)");

  const lookup = directoryFrom(directory);
  const keys = new KeyCache({ teamDomain, fetchImpl, ttlMs: jwksTtlMs, now });

  const audit = (req, decision, detail) => {
    if (typeof onAudit !== "function") return;
    try {
      onAudit({
        at: new Date(now()).toISOString(),
        email: detail.email || null,
        org: detail.org || null,
        role: detail.role || null,
        method: req.method,
        path: req.originalUrl || req.url,
        decision,
        reason: detail.reason || null,
      });
    } catch {
      // Auditing must never be the reason a request fails.
    }
  };

  return async function accessGuard(req, res, next) {
    try {
      const token = readToken(req);
      if (!token) {
        throw unauthenticated("no_token", "no Cloudflare Access token on the request");
      }

      const payload = await verifyAccessToken(token, { keys, teamDomain, aud, now });
      // Normalize once, so the identity we attach is the same string the
      // directory matched on — an audit trail of "  CFO@Pratt.org " helps
      // nobody.
      const email = String(payload.email).trim().toLowerCase();
      const identity = lookup(email);
      if (!identity) {
        // Access let them in the front door; they are still not provisioned
        // for any organization. Deny rather than guess.
        throw denied("not_provisioned", `${email} is not assigned to an organization`);
      }

      req.minerva = { email, org: identity.org, role: identity.role };
      audit(req, "allow", req.minerva);
      next();
    } catch (err) {
      const failure = err instanceof AccessError
        ? err
        : new AccessError(500, "guard_error", err.message);
      audit(req, "deny", { reason: failure.code });
      if (typeof next === "function" && !res) return next(failure);
      res.status(failure.status).json({ error: failure.code, message: failure.message });
    }
  };
}

// --- authorization helpers ------------------------------------------------

/** Express middleware requiring at least `minimum`. */
function requireRole(minimum) {
  if (!isRole(minimum)) throw new Error(`unknown role: ${minimum}`);
  return function roleGuard(req, res, next) {
    const identity = req && req.minerva;
    if (!identity) {
      const err = unauthenticated("no_identity", "accessGuard must run before requireRole");
      return res ? res.status(err.status).json({ error: err.code }) : next(err);
    }
    if (!roleAtLeast(identity.role, minimum)) {
      const err = denied("insufficient_role", `this action needs ${minimum}`);
      return res ? res.status(err.status).json({ error: err.code, message: err.message }) : next(err);
    }
    next();
  };
}

/**
 * The org every query in this request must be filtered by.
 *
 * Returns `null` only for a minerva_admin who has not narrowed to one org —
 * the single case where an unscoped read is legitimate. Every other caller
 * gets a concrete org, and a missing identity throws rather than returning
 * something falsy that a query builder might treat as "no filter".
 */
function orgFilter(req) {
  const identity = req && req.minerva;
  if (!identity) throw unauthenticated("no_identity", "no verified identity on the request");
  if (identity.role === ROLES.MINERVA_ADMIN) return identity.org === "*" ? null : identity.org;
  return identity.org;
}

/**
 * Guard a resource that already carries an org: throws unless it is theirs.
 *
 * Use on the way *out* as well as in — a row fetched by id has to be checked
 * against the caller even when the query that found it looked innocent.
 */
function assertOrg(req, resourceOrg) {
  const identity = req && req.minerva;
  if (!identity) throw unauthenticated("no_identity", "no verified identity on the request");
  if (identity.role === ROLES.MINERVA_ADMIN) return true;
  if (!resourceOrg || resourceOrg !== identity.org) {
    throw denied("wrong_org", "that record belongs to another organization");
  }
  return true;
}

/**
 * Minimum cohort size for peer benchmarks.
 *
 * Peer comparison is the one feature that legitimately reads across orgs, and
 * it stays safe only while a peer cannot be picked out of the cohort. Below
 * this many peers a percentile is effectively a named competitor, so the
 * comparison is withheld instead of shown.
 */
const MIN_PEER_COHORT = 5;

function peerCohortIsSafe(peerCount, minimum = MIN_PEER_COHORT) {
  return Number.isInteger(peerCount) && peerCount >= minimum;
}

module.exports = {
  ROLES,
  AccessError,
  MIN_PEER_COHORT,
  createAccessGuard,
  verifyAccessToken,
  directoryFrom,
  requireRole,
  roleAtLeast,
  isRole,
  orgFilter,
  assertOrg,
  peerCohortIsSafe,
  KeyCache,
};
