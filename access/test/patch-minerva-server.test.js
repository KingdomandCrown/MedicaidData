/**
 * Tests for the server patcher.
 * Run: node --test access/test/patch-minerva-server.test.js
 *
 * A patcher that half-applies is worse than one that refuses, so the properties
 * worth asserting are: every anchor matches, the result is still valid
 * JavaScript, running it twice changes nothing the second time, and a file that
 * has drifted is reported rather than mangled.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const { EDITS, applyAll, checkOllamaGenerate } = require(
  path.join(__dirname, "..", "patch-minerva-server.js"),
);

const FIXTURE = path.join(__dirname, "fixtures", "server-4.0.6.js");
const source = fs.readFileSync(FIXTURE, "utf8");

// --- every anchor matches -------------------------------------------------

test("every edit finds its anchor in the shipped server", () => {
  const { results } = applyAll(source);
  const failed = results.filter((r) => r.status !== "applied");
  assert.deepEqual(failed.map((r) => `${r.id}:${r.status}`), []);
});

test("the patch is idempotent", () => {
  const once = applyAll(source);
  const twice = applyAll(once.text);

  assert.equal(twice.text, once.text);
  assert.deepEqual([...new Set(twice.results.map((r) => r.status))], ["already"]);
});

test("the result is still valid JavaScript", () => {
  // Not a running server -- the requires would need express -- but a syntax
  // error is exactly the failure a string-replacing patcher produces, and it is
  // the one thing that can be checked without the app's dependencies.
  const { text } = applyAll(source);
  assert.doesNotThrow(() => new vm.Script(text, { filename: "patched-server.js" }));
});

// --- the leaks are actually closed ----------------------------------------

test("no process-wide mutable state survives", () => {
  const { text } = applyAll(source);

  // The three declarations are gone...
  assert.equal(/^let uploadedDoc = null;/m.test(text), false);
  assert.equal(/^let lastEnvelope = null;/m.test(text), false);
  assert.equal(/^let lastAgentId = DEFAULT_AGENT;/m.test(text), false);

  // ...and so are the assignments to them. `st.lastEnvelope = ...` and the
  // export route's `const lastEnvelope = st.lastEnvelope` are fine; a bare
  // `lastEnvelope =` at module scope is not.
  assert.equal(/(?<![.\w])uploadedDoc = \{/.test(text), false);
  assert.equal(/(?<![.\w])(?<!const )lastEnvelope = envelope/.test(text), false);
  assert.equal(/(?<![.\w])(?<!const )lastAgentId = agent\.id/.test(text), false);
});

test("every route that takes a ccn checks it", () => {
  const { text } = applyAll(source);
  for (const route of ["/api/hospital", "/api/benchmark", "/api/scorecard", "/api/peers"]) {
    const at = text.indexOf(`app.get("${route}"`);
    assert.notEqual(at, -1, `${route} missing`);
    const body = text.slice(at, at + 260);
    assert.match(body, /scope\.assertCcn\(req, req\.query\.ccn\)/, `${route} unscoped`);
  }
  assert.match(text, /scope\.assertCcn\(req, b\.ccn\)/);   // POST /api/decisions
  assert.match(text, /scope\.assertCcn\(req, rec\.ccn\)/); // POST /api/decisions/verify
  assert.match(text, /scope\.assertCcn\(req, req\.body\.ccn\)/); // POST /api/chat
});

test("the guard is mounted before any route it protects", () => {
  const { text } = applyAll(source);
  const mount = text.indexOf('app.use("/api", access.guard);');
  assert.notEqual(mount, -1);

  const routes = [...text.matchAll(/app\.(get|post)\("\/api\//g)].map((m) => m.index);
  assert.equal(routes.length > 0, true);
  assert.equal(Math.min(...routes) > mount, true, "a route is registered before the guard");
});

test("health no longer publishes the last uploaded filename", () => {
  const { text } = applyAll(source);
  const at = text.indexOf('app.get("/health"');
  const body = text.slice(at, text.indexOf("}));", at));

  assert.equal(body.includes("uploadedDoc"), false);
  assert.match(body, /callers: tenants\.stats\(\)\.callers/);
});

test("the server binds to loopback, because Access protects a hostname not a port", () => {
  const { text } = applyAll(source);
  assert.match(text, /app\.listen\(PORT, HOST,/);
  assert.equal(/app\.listen\(PORT, \(\)/.test(text), false);
});

test("the error handler is registered after the routes", () => {
  const { text } = applyAll(source);
  const handler = text.indexOf("app.use(access.accessErrorHandler);");
  const lastRoute = text.lastIndexOf('app.get("/api/');
  assert.notEqual(handler, -1);
  assert.equal(handler > lastRoute, true);
});

test("the internal model bake-off is closed to customers", () => {
  const { text } = applyAll(source);
  assert.match(text, /app\.post\("\/api\/bakeoff-one", requireMinervaAdmin,/);
  assert.match(text, /app\.post\("\/api\/bakeoff", requireMinervaAdmin,/);
});

// --- a file that has drifted is reported, not mangled ----------------------

test("a missing anchor is reported and does not half-apply", () => {
  const drifted = source.replace(
    'app.get("/api/scorecard", (req, res) => {',
    'app.get("/api/scorecard", async (req, res) => {',
  );
  const { results } = applyAll(drifted);
  const miss = results.filter((r) => r.status === "missing").map((r) => r.id);

  assert.deepEqual(miss, ["scorecard-scope"]);
});

test("an anchor appearing an unexpected number of times is refused", () => {
  const doubled = source.replace(
    "  res.json({ hospitals: vault.list(), version: vault.version() });",
    "  res.json({ hospitals: vault.list(), version: vault.version() });\n" +
    "  res.json({ hospitals: vault.list(), version: vault.version() });",
  );
  const { results } = applyAll(doubled);
  const row = results.find((r) => r.id === "hospital-picker");

  assert.equal(row.status, "ambiguous");
  assert.equal(row.found, 2);
});

test("edit ids are unique, so the report cannot be misread", () => {
  const ids = EDITS.map((e) => e.id);
  assert.equal(new Set(ids).size, ids.length);
});

test("every edit explains itself", () => {
  for (const edit of EDITS) {
    assert.equal(typeof edit.why, "string", `${edit.id} has no reason`);
    assert.equal(edit.why.length > 20, true, `${edit.id}: "${edit.why}"`);
  }
});

// --- the unrelated bug ----------------------------------------------------

test("a call with no definition is detected", () => {
  assert.deepEqual(
    checkOllamaGenerate("const x = await ollamaGenerate(p);"),
    { called: true, defined: false },
  );
  assert.deepEqual(
    checkOllamaGenerate("async function ollamaGenerate(p) {}\nollamaGenerate(1);"),
    { called: true, defined: true },
  );
});
