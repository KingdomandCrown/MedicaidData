/**
 * Tests for MinervaAI tenant + role access control.
 * Run: node --test access/test/
 *
 * The interesting cases here are the denials. A signed token is easy; what
 * protects a pilot is that a forged header, a token for another Access app, an
 * expired token, or an email nobody provisioned all fail closed.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const crypto = require("node:crypto");
const path = require("path");

const A = require(path.join(__dirname, "..", "access-control.js"));

// --- test rig -------------------------------------------------------------

const TEAM = "minervaai.cloudflareaccess.com";
const AUD = "a1b2c3d4e5f6";
const NOW_MS = 1_766_000_000_000;
const nowSec = Math.floor(NOW_MS / 1000);
const now = () => NOW_MS;

const { publicKey, privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
const other = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });

const b64url = (buf) =>
  Buffer.from(buf).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function sign(payload, opts = {}) {
  const header = { alg: opts.alg || "RS256", kid: opts.kid || "key-1", typ: "JWT" };
  const h = b64url(JSON.stringify(header));
  const p = b64url(JSON.stringify(payload));
  if (opts.alg === "none") return `${h}.${p}.`;
  const sig = crypto.sign("RSA-SHA256", Buffer.from(`${h}.${p}`), opts.key || privateKey);
  return `${h}.${p}.${b64url(sig)}`;
}

function claims(overrides = {}) {
  return Object.assign(
    {
      iss: `https://${TEAM}`,
      aud: [AUD],
      email: "cfo@prattregional.org",
      exp: nowSec + 3600,
      iat: nowSec - 10,
    },
    overrides,
  );
}

function jwks(keys) {
  return {
    keys: (keys || [{ kid: "key-1", key: publicKey }]).map(({ kid, key }) =>
      Object.assign(key.export({ format: "jwk" }), { kid, alg: "RS256", use: "sig" }),
    ),
  };
}

/** A fetch stand-in that counts calls, so cache behaviour is observable. */
function fakeFetch(body, { ok = true, status = 200 } = {}) {
  const fn = async () => {
    fn.calls += 1;
    return { ok, status, json: async () => (typeof body === "function" ? body() : body) };
  };
  fn.calls = 0;
  return fn;
}

const DIRECTORY = {
  "cfo@prattregional.org": { org: "pratt", role: A.ROLES.ORG_ADMIN },
  "analyst@prattregional.org": { org: "pratt", role: A.ROLES.ORG_MEMBER },
  "nurse@hutchregional.org": { org: "hutchinson", role: A.ROLES.ORG_MEMBER },
  "jeff@minervaai.health": { org: "*", role: A.ROLES.MINERVA_ADMIN },
};

function guard(overrides = {}) {
  return A.createAccessGuard(
    Object.assign(
      {
        teamDomain: TEAM,
        aud: AUD,
        directory: DIRECTORY,
        fetchImpl: fakeFetch(jwks()),
        now,
      },
      overrides,
    ),
  );
}

/** Minimal req/res doubles. `run` resolves with what the middleware did. */
function request(headers = {}, extra = {}) {
  return Object.assign({ method: "GET", url: "/api/scorecard", headers }, extra);
}

async function run(middleware, req) {
  return new Promise((resolve) => {
    const res = {
      statusCode: null,
      body: null,
      status(code) { this.statusCode = code; return this; },
      json(payload) { this.body = payload; resolve({ passed: false, res: this, req }); return this; },
    };
    middleware(req, res, (err) => resolve({ passed: !err, err, res, req }));
  });
}

const withToken = (token) => request({ "cf-access-jwt-assertion": token });

// --- happy path -----------------------------------------------------------

test("a valid token attaches the org and role", async () => {
  const out = await run(guard(), withToken(sign(claims())));
  assert.equal(out.passed, true);
  assert.deepEqual(out.req.minerva, {
    email: "cfo@prattregional.org",
    org: "pratt",
    role: A.ROLES.ORG_ADMIN,
  });
});

test("the token is also accepted from the Access cookie", async () => {
  const req = request({ cookie: `foo=bar; CF_Authorization=${sign(claims())}; baz=1` });
  const out = await run(guard(), req);
  assert.equal(out.passed, true);
  assert.equal(out.req.minerva.org, "pratt");
});

test("email comparison ignores case and surrounding space", async () => {
  const out = await run(guard(), withToken(sign(claims({ email: "  CFO@PrattRegional.org " }))));
  assert.equal(out.passed, true);
  assert.equal(out.req.minerva.email, "cfo@prattregional.org");
});

// --- the denials that matter ----------------------------------------------

test("a spoofed email header alone gets nobody in", async () => {
  // The header Cloudflare sets is trivially forged by anything that can reach
  // the origin directly. Only the signed assertion counts.
  const req = request({ "cf-access-authenticated-user-email": "jeff@minervaai.health" });
  const out = await run(guard(), req);
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "no_token");
});

test("a token signed by someone else is rejected", async () => {
  const forged = sign(claims(), { key: other.privateKey });
  const out = await run(guard(), withToken(forged));
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "bad_signature");
});

test("an unsigned 'alg: none' token is rejected", async () => {
  const out = await run(guard(), withToken(sign(claims(), { alg: "none" })));
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "bad_algorithm");
});

test("a token for a different Access application is rejected", async () => {
  // Same team, same signing key, different app — signature alone is not enough.
  const out = await run(guard(), withToken(sign(claims({ aud: ["some-other-app"] }))));
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "bad_audience");
});

test("a token from another team is rejected", async () => {
  const out = await run(guard(), withToken(sign(claims({ iss: "https://evil.cloudflareaccess.com" }))));
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "bad_issuer");
});

test("an expired token is rejected", async () => {
  const out = await run(guard(), withToken(sign(claims({ exp: nowSec - 3600 }))));
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "expired");
});

test("a token that is not valid yet is rejected", async () => {
  const out = await run(guard(), withToken(sign(claims({ nbf: nowSec + 3600 }))));
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "not_yet_valid");
});

test("an approved email with no org assignment is denied, not defaulted", async () => {
  // Access let them through the front door; MinervaAI still has no idea whose
  // data they should see. That must be a 403, never a guess.
  const out = await run(guard(), withToken(sign(claims({ email: "stranger@example.com" }))));
  assert.equal(out.res.statusCode, 403);
  assert.equal(out.res.body.error, "not_provisioned");
});

test("garbage in the token position is rejected", async () => {
  for (const junk of ["", "not-a-jwt", "a.b", "a.b.c.d"]) {
    const out = await run(guard(), withToken(junk));
    assert.equal(out.res.statusCode, 401, `expected 401 for ${JSON.stringify(junk)}`);
  }
});

test("an unreachable certs endpoint denies rather than allows", async () => {
  const g = guard({ fetchImpl: fakeFetch(null, { ok: false, status: 503 }) });
  const out = await run(g, withToken(sign(claims())));
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "keys_unavailable");
});

// --- key handling ---------------------------------------------------------

test("keys are cached across requests", async () => {
  const fetchImpl = fakeFetch(jwks());
  const g = guard({ fetchImpl });
  await run(g, withToken(sign(claims())));
  await run(g, withToken(sign(claims())));
  assert.equal(fetchImpl.calls, 1);
});

test("an unknown kid triggers one refresh, then denies", async () => {
  const fetchImpl = fakeFetch(jwks());
  const g = guard({ fetchImpl });
  await run(g, withToken(sign(claims())));            // primes the cache
  const out = await run(g, withToken(sign(claims(), { kid: "rotated" })));
  assert.equal(fetchImpl.calls, 2, "should re-fetch once in case keys rotated");
  assert.equal(out.res.statusCode, 401);
  assert.equal(out.res.body.error, "unknown_key");
});

test("a rotated key is picked up on refresh", async () => {
  let served = jwks();
  const fetchImpl = fakeFetch(() => served);
  const g = guard({ fetchImpl });
  await run(g, withToken(sign(claims())));

  served = jwks([{ kid: "key-2", key: other.publicKey }]);
  const out = await run(g, withToken(sign(claims(), { kid: "key-2", key: other.privateKey })));
  assert.equal(out.passed, true);
});

// --- roles ----------------------------------------------------------------

test("role ranking is ordered", () => {
  assert.equal(A.roleAtLeast(A.ROLES.ORG_ADMIN, A.ROLES.ORG_MEMBER), true);
  assert.equal(A.roleAtLeast(A.ROLES.ORG_MEMBER, A.ROLES.ORG_ADMIN), false);
  assert.equal(A.roleAtLeast(A.ROLES.MINERVA_ADMIN, A.ROLES.ORG_ADMIN), true);
  assert.equal(A.roleAtLeast("made_up", A.ROLES.ORG_VIEWER), false);
});

test("requireRole lets a sufficient role through and blocks the rest", async () => {
  const admin = { minerva: { email: "a@b.c", org: "pratt", role: A.ROLES.ORG_ADMIN } };
  const member = { minerva: { email: "d@e.f", org: "pratt", role: A.ROLES.ORG_MEMBER } };
  const mw = A.requireRole(A.ROLES.ORG_ADMIN);

  assert.equal((await run(mw, request({}, admin))).passed, true);
  const blocked = await run(mw, request({}, member));
  assert.equal(blocked.res.statusCode, 403);
  assert.equal(blocked.res.body.error, "insufficient_role");
});

test("requireRole without a verified identity denies", async () => {
  const out = await run(A.requireRole(A.ROLES.ORG_VIEWER), request());
  assert.equal(out.res.statusCode, 401);
});

// --- scoping --------------------------------------------------------------

test("orgFilter returns the caller's org", () => {
  const req = { minerva: { email: "a@b.c", org: "pratt", role: A.ROLES.ORG_MEMBER } };
  assert.equal(A.orgFilter(req), "pratt");
});

test("orgFilter throws without an identity rather than returning nothing", () => {
  // A falsy return here would read as "no filter" in a query builder, which is
  // exactly the bug this is meant to prevent.
  assert.throws(() => A.orgFilter({}), /no verified identity/);
});

test("only minerva_admin gets an unscoped read", () => {
  const staff = { minerva: { email: "j@m.h", org: "*", role: A.ROLES.MINERVA_ADMIN } };
  assert.equal(A.orgFilter(staff), null);
  const scopedStaff = { minerva: { email: "j@m.h", org: "pratt", role: A.ROLES.MINERVA_ADMIN } };
  assert.equal(A.orgFilter(scopedStaff), "pratt");
});

test("assertOrg blocks reading another hospital's record", () => {
  const pratt = { minerva: { email: "a@b.c", org: "pratt", role: A.ROLES.ORG_ADMIN } };
  assert.equal(A.assertOrg(pratt, "pratt"), true);
  assert.throws(() => A.assertOrg(pratt, "hutchinson"), /another organization/);
  assert.throws(() => A.assertOrg(pratt, null), /another organization/);
});

test("minerva_admin may cross orgs", () => {
  const staff = { minerva: { email: "j@m.h", org: "*", role: A.ROLES.MINERVA_ADMIN } };
  assert.equal(A.assertOrg(staff, "hutchinson"), true);
});

// --- directory ------------------------------------------------------------

test("a directory entry with an unknown role is rejected at build time", () => {
  assert.throws(
    () => A.directoryFrom({ "a@b.c": { org: "pratt", role: "superuser" } }),
    /unknown role/,
  );
});

test("a directory entry missing org or role is rejected", () => {
  assert.throws(() => A.directoryFrom({ "a@b.c": { org: "pratt" } }), /needs both/);
  assert.throws(() => A.directoryFrom({ "a@b.c": { role: A.ROLES.ORG_MEMBER } }), /needs both/);
});

test("comment keys in the directory file are skipped", () => {
  // JSON has no comments, so the shipped example carries a "_comment" key.
  // Reading it as a person made the example file impossible to load.
  const lookup = A.directoryFrom({
    _comment: ["notes for whoever edits this"],
    "cfo@prattregional.org": { org: "pratt", role: A.ROLES.ORG_ADMIN },
  });
  assert.deepEqual(lookup("cfo@prattregional.org"), { org: "pratt", role: A.ROLES.ORG_ADMIN });
  assert.equal(lookup("_comment"), null);
});

test("the shipped example directory actually loads", () => {
  const example = require(path.join(__dirname, "..", "directory.example.json"));
  const lookup = A.directoryFrom(example);
  assert.equal(lookup("jeff@minervaai.health").role, A.ROLES.MINERVA_ADMIN);
});

test("only minerva_admin may hold the wildcard org", () => {
  assert.throws(
    () => A.directoryFrom({ "a@b.c": { org: "*", role: A.ROLES.ORG_ADMIN } }),
    /only minerva_admin/,
  );
});

// --- peer benchmarks ------------------------------------------------------

test("a peer cohort below the minimum is withheld", () => {
  // With four peers a percentile is a named competitor with extra steps.
  assert.equal(A.peerCohortIsSafe(4), false);
  assert.equal(A.peerCohortIsSafe(5), true);
  assert.equal(A.peerCohortIsSafe(0), false);
  assert.equal(A.peerCohortIsSafe(null), false);
});

// --- audit ----------------------------------------------------------------

test("allowed and denied requests are both audited", async () => {
  const entries = [];
  const g = guard({ onAudit: (e) => entries.push(e) });

  await run(g, withToken(sign(claims())));
  await run(g, withToken(sign(claims({ email: "stranger@example.com" }))));

  assert.equal(entries.length, 2);
  assert.equal(entries[0].decision, "allow");
  assert.equal(entries[0].org, "pratt");
  assert.equal(entries[0].path, "/api/scorecard");
  assert.equal(entries[1].decision, "deny");
  assert.equal(entries[1].reason, "not_provisioned");
});

test("a throwing audit hook does not fail the request", async () => {
  const g = guard({ onAudit: () => { throw new Error("log sink down"); } });
  const out = await run(g, withToken(sign(claims())));
  assert.equal(out.passed, true);
});

// --- config ---------------------------------------------------------------

test("the guard refuses to start without an audience", () => {
  assert.throws(() => A.createAccessGuard({ teamDomain: TEAM, directory: {} }), /needs aud/);
  assert.throws(() => A.createAccessGuard({ aud: AUD, directory: {} }), /needs teamDomain/);
});
