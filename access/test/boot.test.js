/**
 * Tests for the one-call assembly.
 * Run: node --test access/test/boot.test.js
 *
 * The behaviour that matters here is the refusal. A misconfigured server that
 * boots and serves is the failure this whole directory exists to prevent, so
 * "no configuration" must not be a path to a running process.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { boot, accessErrorHandler } = require(path.join(__dirname, "..", "boot.js"));
const { AccessError } = require(path.join(__dirname, "..", "access-control.js"));

const DIRECTORY = {
  _comment: "ignored",
  "cfo@prattregional.org": { org: "pratt", role: "org_admin" },
  "jeff@minervaai.health": { org: "*", role: "minerva_admin" },
};
const ORGS = { pratt: { name: "Pratt Regional Medical Center", ccns: ["170027"] } };

function appDir(overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "minerva-boot-"));
  fs.mkdirSync(path.join(dir, "access"));
  const files = Object.assign({ "directory.json": DIRECTORY, "orgs.json": ORGS }, overrides);
  for (const [name, body] of Object.entries(files)) {
    if (body === null) continue;
    fs.writeFileSync(path.join(dir, "access", name), JSON.stringify(body, null, 2));
  }
  return dir;
}

const quiet = (fn) => {
  const log = console.log;
  console.log = () => {};
  try { return fn(); } finally { console.log = log; }
};

const DEV = { MINERVA_DEV_IDENTITY: "cfo@prattregional.org" };

// --- refusing to start ----------------------------------------------------

test("with no Access configuration the server does not start", () => {
  const dir = appDir();
  assert.throws(
    () => boot({ appDir: dir, env: {} }),
    /will not start/,
  );
});

test("the refusal names the two variables to set", () => {
  const dir = appDir();
  const err = (() => { try { boot({ appDir: dir, env: {} }); } catch (e) { return e; } })();
  assert.match(err.message, /ACCESS_TEAM_DOMAIN/);
  assert.match(err.message, /ACCESS_AUD/);
});

test("half a configuration is no configuration", () => {
  const dir = appDir();
  assert.throws(() => boot({ appDir: dir, env: { ACCESS_AUD: "abc" } }), /will not start/);
  assert.throws(
    () => boot({ appDir: dir, env: { ACCESS_TEAM_DOMAIN: "t.cloudflareaccess.com" } }),
    /will not start/,
  );
});

test("the development bypass is refused in production", () => {
  const dir = appDir();
  assert.throws(
    () => boot({ appDir: dir, env: Object.assign({ NODE_ENV: "production" }, DEV) }),
    /will not start/,
  );
});

test("a development identity that the directory would deny is refused", () => {
  // Otherwise the dev server grants an identity the real one does not, and the
  // first thing anyone discovers in production is a 403 they never saw locally.
  const dir = appDir();
  assert.throws(
    () => boot({ appDir: dir, env: { MINERVA_DEV_IDENTITY: "nobody@example.com" } }),
    /not in directory\.json/,
  );
});

// --- the configuration files ----------------------------------------------

test("a missing config file says which one and how to create it", () => {
  const dir = appDir({ "orgs.json": null });
  const err = (() => { try { boot({ appDir: dir, env: DEV }); } catch (e) { return e; } })();
  assert.match(err.message, /orgs\.json not found/);
  assert.match(err.message, /orgs\.example\.json/);
});

test("malformed JSON is reported as such, not as a crash", () => {
  const dir = appDir();
  fs.writeFileSync(path.join(dir, "access", "orgs.json"), "{ not json");
  assert.throws(() => boot({ appDir: dir, env: DEV }), /not valid JSON/);
});

test("a config error in orgs.json stops the boot", () => {
  const dir = appDir({ "orgs.json": { a: { ccns: ["170027"] }, b: { ccns: ["170027"] } } });
  assert.throws(() => boot({ appDir: dir, env: DEV }), /listed under both/);
});

// --- what it hands back ---------------------------------------------------

test("development mode assembles a working guard, scope, and state store", () => {
  const dir = appDir();
  const access = quiet(() => boot({ appDir: dir, defaultAgent: "ask-minerva", env: DEV }));

  assert.equal(access.mode, "development");

  const req = { headers: {} };
  const warn = console.warn;
  console.warn = () => {};
  try { access.guard(req, {}, () => {}); } finally { console.warn = warn; }

  assert.deepEqual(req.minerva, {
    email: "cfo@prattregional.org", org: "pratt", role: "org_admin",
  });
  assert.equal(access.scope.allows(req, "170027"), true);
  assert.equal(access.scope.allows(req, "170045"), false);
  assert.equal(access.tenants.for(req).lastAgentId, "ask-minerva");
});

test("a real Access configuration builds the verifying guard", () => {
  const dir = appDir();
  const access = quiet(() => boot({
    appDir: dir,
    env: { ACCESS_TEAM_DOMAIN: "minervaai.cloudflareaccess.com", ACCESS_AUD: "abc123" },
  }));

  assert.equal(access.mode, "cloudflare-access");
  assert.equal(typeof access.guard, "function");
});

test("the audit log records who asked for what, and how it was answered", async () => {
  // Which hospital was requested is the resource, not the data — an audit trail
  // that omits it cannot answer the only question it will ever be asked. What
  // must stay out is the response: a log of what was *in* the scorecard is a
  // second copy of the data, kept somewhere with weaker protection than the
  // first. The guard only ever sees the request, so this holds by construction.
  const dir = appDir();
  const access = quiet(() => boot({
    appDir: dir,
    env: { ACCESS_TEAM_DOMAIN: "t.cloudflareaccess.com", ACCESS_AUD: "abc" },
  }));

  const res = { status: () => res, json: () => res };
  await access.guard({ headers: {}, method: "GET", url: "/api/scorecard?ccn=170027" }, res);

  const entry = JSON.parse(fs.readFileSync(path.join(dir, "access-audit.log"), "utf8").trim());
  assert.equal(entry.decision, "deny");
  assert.equal(entry.reason, "no_token");
  assert.equal(entry.path, "/api/scorecard?ccn=170027");
  assert.deepEqual(
    Object.keys(entry).sort(),
    ["at", "decision", "email", "method", "org", "path", "reason", "role"],
  );
});

test("a request that cannot be written to the log still fails closed", () => {
  // Auditing must never be the reason a request is allowed *or* denied
  // differently. An unwritable log directory is an ops problem, not a bypass.
  const dir = appDir();
  const access = quiet(() => boot({
    appDir: dir,
    env: { ACCESS_TEAM_DOMAIN: "t.cloudflareaccess.com", ACCESS_AUD: "abc" },
  }));
  fs.rmSync(dir, { recursive: true, force: true });

  let status = null;
  const res = { status: (s) => { status = s; return res; }, json: () => res };
  return access.guard({ headers: {}, method: "GET", url: "/api/scorecard" }, res)
    .then(() => assert.equal(status, 401));
});

// --- the error handler ----------------------------------------------------

test("a denial becomes a 403 rather than a 500", () => {
  let status = null;
  let body = null;
  const res = { headersSent: false, status: (s) => { status = s; return res; }, json: (b) => { body = b; } };

  accessErrorHandler(new AccessError(403, "ccn_out_of_scope", "not yours"), {}, res, () => {
    throw new Error("should not reach next");
  });

  assert.equal(status, 403);
  assert.equal(body.error, "ccn_out_of_scope");
});

test("an ordinary crash is left alone for the ordinary handler", () => {
  let passed = false;
  const res = { headersSent: false, status: () => res, json: () => {} };
  accessErrorHandler(new TypeError("boom"), {}, res, () => { passed = true; });
  assert.equal(passed, true);
});

test("an error after the response started is passed on, not written twice", () => {
  let passed = false;
  const res = { headersSent: true, status: () => res, json: () => {} };
  accessErrorHandler(new AccessError(403, "x", "y"), {}, res, () => { passed = true; });
  assert.equal(passed, true);
});
