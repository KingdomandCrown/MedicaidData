"use strict";

/**
 * Per-caller state, replacing module-level variables in a multi-tenant server.
 *
 * `minerva-prod/server.js` keeps three values at module scope:
 *
 *     let uploadedDoc  = null;   // { name, text }
 *     let lastEnvelope = null;   // last validated scorecard, used by /api/export
 *     let lastAgentId  = DEFAULT_AGENT;
 *
 * One copy each, for the whole process. That is correct for a single-user demo
 * and wrong the moment a second organization signs in:
 *
 *   - `uploadedDoc` is spliced into the system prompt of whoever chats next, so
 *     one client's document becomes context in another client's conversation.
 *   - `lastEnvelope` is what `/api/export` writes into a .docx, so a client can
 *     download a memo built from another client's scorecard.
 *   - `/health` reports the uploaded document's *filename*, unauthenticated.
 *
 * None of these require a mistake by a user to leak. They leak by working as
 * written. Access control on the front door does not help: both callers are
 * legitimately signed in, and they are reading one variable.
 *
 * This store keys that state by **caller**, not by organization. Per-org would
 * close the cross-tenant hole, but two colleagues at the same hospital would
 * still find each other's uploads appearing mid-conversation — surprising, and
 * needless when the finer key costs nothing. Isolation per caller implies
 * isolation per organization.
 *
 * Entries expire: a chat server that never forgets an upload accumulates the
 * full text of every document any client has ever sent, in memory, until it is
 * restarted.
 */

const DEFAULT_TTL_MS = 8 * 60 * 60 * 1000; // a working day
const DEFAULT_MAX_CALLERS = 500;

/**
 * The verified identity on a request.
 *
 * `access-control.js` attaches `req.minerva = { email, org, role }`. Earlier
 * drafts of this file assumed a generic `req.user = { sub, orgId }`, which read
 * fine on its own and threw on every request once the two were wired together —
 * the guard never sets `req.user`. Both shapes are accepted now, and `minerva`
 * wins, so this cannot drift apart from the guard again.
 */
function identityOf(req) {
  return (req && (req.minerva || req.user)) || null;
}

class TenantState {
  /**
   * @param {object} [options]
   * @param {() => object} [options.defaults] fresh state for a new caller
   * @param {number} [options.ttlMs] idle time before an entry is dropped
   * @param {number} [options.maxCallers] hard cap; oldest are evicted first
   * @param {() => number} [options.now] clock, for tests
   */
  constructor(options = {}) {
    const {
      defaults = () => ({}),
      ttlMs = DEFAULT_TTL_MS,
      maxCallers = DEFAULT_MAX_CALLERS,
      now = Date.now,
    } = options;

    this._defaults = defaults;
    this._ttlMs = ttlMs;
    this._maxCallers = maxCallers;
    this._now = now;
    this._entries = new Map(); // key -> { state, org, touchedAt }
  }

  /**
   * The identity this request's state belongs to.
   *
   * Throws rather than falling back to a shared bucket: an unauthenticated
   * request reaching here means the guard is missing from a route, and quietly
   * handing back shared state would restore the exact bug this replaces.
   */
  keyFor(req) {
    const user = identityOf(req);
    const key = user && (user.sub || user.email);
    if (!key) {
      throw new Error(
        "tenant state requires an authenticated request — is the access guard " +
          "mounted on this route?"
      );
    }
    return String(key);
  }

  /** This caller's state, created on first use. Mutate the returned object. */
  for(req) {
    const key = this.keyFor(req);
    const now = this._now();
    let entry = this._entries.get(key);

    if (entry && now - entry.touchedAt > this._ttlMs) {
      this._entries.delete(key);
      entry = undefined;
    }
    if (!entry) {
      const identity = identityOf(req) || {};
      entry = {
        state: this._defaults(),
        org: identity.org || identity.orgId || null,
        touchedAt: now,
      };
      this._entries.set(key, entry);
    } else {
      // Re-insert so Map iteration order tracks least-recently-used.
      this._entries.delete(key);
      entry.touchedAt = now;
      this._entries.set(key, entry);
    }

    this._evictIfNeeded();
    return entry.state;
  }

  /** Forget this caller's state — sign-out, or an explicit "clear document". */
  clear(req) {
    return this._entries.delete(this.keyFor(req));
  }

  /** Drop everything belonging to one organization. */
  clearOrg(orgId) {
    let dropped = 0;
    for (const [key, entry] of this._entries) {
      if (entry.org === orgId) {
        this._entries.delete(key);
        dropped += 1;
      }
    }
    return dropped;
  }

  /** Drop expired entries. Safe to call on a timer. */
  sweep() {
    const now = this._now();
    let dropped = 0;
    for (const [key, entry] of this._entries) {
      if (now - entry.touchedAt > this._ttlMs) {
        this._entries.delete(key);
        dropped += 1;
      }
    }
    return dropped;
  }

  _evictIfNeeded() {
    while (this._entries.size > this._maxCallers) {
      const oldest = this._entries.keys().next();
      if (oldest.done) break;
      this._entries.delete(oldest.value);
    }
  }

  get size() {
    return this._entries.size;
  }

  /**
   * Counts only — for /health and logs.
   *
   * Deliberately returns no key, no organization, and nothing about the
   * documents held. The route this replaces published the uploaded file's
   * name to anyone who asked.
   */
  stats() {
    return { callers: this._entries.size };
  }
}

module.exports = { TenantState, identityOf, DEFAULT_TTL_MS, DEFAULT_MAX_CALLERS };
