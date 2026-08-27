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

const ALL = { enable: ["--fix-export"] };

test("every edit finds its anchor in the shipped server", () => {
  const { results } = applyAll(source, ALL);
  // "skip" is an edit belonging to the other mode, not a failure to match.
  const failed = results.filter((r) => !["applied", "unneeded", "skip"].includes(r.status));
  assert.deepEqual(failed.map((r) => `${r.id}:${r.status}`), []);
});

test("the patch is idempotent", () => {
  const once = applyAll(source, ALL);
  const twice = applyAll(once.text, ALL);

  assert.equal(twice.text, once.text);
  assert.deepEqual(
    [...new Set(twice.results.map((r) => r.status))].sort(),
    ["already", "skip", "unneeded"],
  );
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

// --- the unrelated fix stays opt-in ---------------------------------------

test("an unrelated fix is off unless it is asked for", () => {
  // A security patch that quietly carries a functional change is a patch nobody
  // can review as either one.
  const row = applyAll(source).results.find((r) => r.id === "export-ollama-generate");
  assert.equal(row.status, "off");
});

test("a definition that already exists is not shadowed by a second one", () => {
  // The fixture defines ollamaGenerate. Adding another would replace working
  // code with a guess at what it does.
  const row = applyAll(source, ALL).results.find((r) => r.id === "export-ollama-generate");
  assert.equal(row.status, "unneeded");
  assert.equal((applyAll(source, ALL).text.match(/async function ollamaGenerate/g) || []).length, 1);
});

test("where the definition is genuinely missing, it is added and still parses", () => {
  const broken = source.replace(
    'async function ollamaGenerate(prompt, useSchema, model) { return ""; }\n',
    "",
  );
  assert.equal(/function ollamaGenerate/.test(broken), false, "fixture edit did not take");

  const { text, results } = applyAll(broken, ALL);
  assert.equal(results.find((r) => r.id === "export-ollama-generate").status, "applied");
  assert.match(text, /return ollamaStream\(prompt, useSchema, null, null, model\);/);
  assert.doesNotThrow(() => new vm.Script(text, { filename: "patched.js" }));
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

// --- hardening without the access control ---------------------------------

const HARD = { mode: "hardening", enable: ["--fix-export"] };

test("hardening-only applies the fixes that are not about tenancy", () => {
  const { results } = applyAll(source, HARD);
  const applied = results.filter((r) => r.status === "applied").map((r) => r.id);

  assert.deepEqual(applied.sort(), ["health-hardening", "listen-loopback-hardening"]);
});

test("hardening-only names nothing from the access modules at all", () => {
  // The bug this exists for: listen-loopback was shared between both modes and
  // its replacement carried `app.use(access.accessErrorHandler)`. In hardening
  // mode there is no `access` object, so the patched server threw
  // ReferenceError on startup and pm2 restarted it in a loop -- reported as
  // "online", answering nothing.
  //
  // Asserting on the four names I happened to think of is what let it through.
  // Assert on the prefix instead.
  const { text } = applyAll(source, HARD);
  const leaked = [...text.matchAll(/\baccess\.[a-zA-Z]+/g)].map((m) => m[0]);

  assert.deepEqual([...new Set(leaked)], []);
  assert.equal(/\bbootAccess\b|\bscope\.|\btenants\./.test(text), false);
});

test("nothing the patcher introduces is used without also being declared", () => {
  // The general form of the bug: an edit in one mode names something only the
  // other mode defines. A syntax check cannot see it -- `access.foo` parses
  // fine when `access` is undefined; it throws at startup, and pm2 reports the
  // crash loop as "online".
  const INTRODUCED = {
    access: "const access = bootAccess(",
    scope: "const scope = access.scope;",
    tenants: "const tenants = access.tenants;",
    requireMinervaAdmin: "const requireMinervaAdmin =",
    bootAccess: 'require("./access/boot")',
  };

  for (const opts of [HARD, ALL]) {
    const { text } = applyAll(source, opts);
    for (const [name, declaration] of Object.entries(INTRODUCED)) {
      const used = new RegExp(`(?<![\\w.$])${name}\\s*[.(]`).test(text);
      if (!used) continue;
      assert.equal(
        text.includes(declaration),
        true,
        `${opts.mode || "access"} mode uses ${name} without ${declaration}`,
      );
    }
  }
});

test("hardening-only leaves the server free of the access modules", () => {
  // Cloudflare decides who gets in. If any of this survived, the server would
  // require configuration that is deliberately not there and fail to start.
  const { text } = applyAll(source, HARD);

  assert.equal(text.includes("bootAccess"), false);
  assert.equal(text.includes("scope.assertCcn"), false);
  assert.equal(text.includes("tenants.for(req)"), false);
  assert.equal(text.includes("access.guard"), false);
  assert.doesNotThrow(() => new vm.Script(text, { filename: "hardened.js" }));
});

test("hardening-only still closes the origin and the health leak", () => {
  const { text } = applyAll(source, HARD);

  assert.match(text, /app\.listen\(PORT, HOST,/);
  const at = text.indexOf('app.get("/health"');
  assert.equal(text.slice(at, text.indexOf("}));", at)).includes("uploadedDoc"), false);
});

test("the two health edits never both apply", () => {
  // They rewrite the same route. Whichever ran first would leave the other
  // matching nothing, which reads as drift rather than as a mode.
  for (const opts of [HARD, { mode: "access" }]) {
    const applied = applyAll(source, opts).results
      .filter((r) => r.status === "applied")
      .map((r) => r.id)
      .filter((id) => id.startsWith("health"));
    assert.equal(applied.length, 1, `${opts.mode}: ${applied.join()}`);
  }
});

test("hardening-only is idempotent too", () => {
  const once = applyAll(source, HARD);
  assert.equal(applyAll(once.text, HARD).text, once.text);
});

// --- putting it back ------------------------------------------------------

const { originalBackup, revert } = require(
  path.join(__dirname, "..", "patch-minerva-server.js"),
);

function scratch(t) {
  const os = require("node:os");
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "revert-"));
  const server = path.join(dir, "server.js");
  fs.writeFileSync(server, source);
  return { dir, server };
}

const quiet = (fn) => {
  const log = console.log;
  console.log = () => {};
  try { return fn(); } finally { console.log = log; }
};

test("the oldest backup is the original, not the newest", () => {
  // Every --apply writes one, so after a few rounds only the first is the
  // untouched file. Reverting to the newest would look like it worked and
  // change nothing.
  const { dir, server } = scratch();
  fs.writeFileSync(server + ".bak-2026-08-26T23-55-38-249Z", "ORIGINAL");
  fs.writeFileSync(server + ".bak-2026-08-27T00-25-31-354Z", "ALREADY PATCHED");

  assert.equal(path.basename(originalBackup(server)), "server.js.bak-2026-08-26T23-55-38-249Z");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("reverting restores the file the patcher first saw", () => {
  const { dir, server } = scratch();
  const patched = applyAll(source, ALL).text;
  fs.writeFileSync(server + ".bak-2026-08-26T23-55-38-249Z", source);
  fs.writeFileSync(server, patched);

  assert.equal(quiet(() => revert(server)), 0);
  assert.equal(fs.readFileSync(server, "utf8"), source);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("reverting an already-reverted file changes nothing", () => {
  const { dir, server } = scratch();
  fs.writeFileSync(server + ".bak-2026-08-26T23-55-38-249Z", source);

  assert.equal(quiet(() => revert(server)), 0);
  assert.equal(fs.readFileSync(server, "utf8"), source);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("reverting with no backup refuses rather than guessing", () => {
  const { dir, server } = scratch();
  const err = console.error;
  console.error = () => {};
  try {
    assert.equal(revert(server), 1);
  } finally {
    console.error = err;
  }
  assert.equal(fs.readFileSync(server, "utf8"), source);
  fs.rmSync(dir, { recursive: true, force: true });
});

test("a reverted file takes the hardening patch cleanly", () => {
  // The path out of the full patch and into Cloudflare-only.
  const { dir, server } = scratch();
  fs.writeFileSync(server + ".bak-2026-08-26T23-55-38-249Z", source);
  fs.writeFileSync(server, applyAll(source, ALL).text);
  quiet(() => revert(server));

  const { text, results } = applyAll(fs.readFileSync(server, "utf8"), HARD);
  // The fixture already defines ollamaGenerate, so that edit is "unneeded"
  // rather than applied — the real server.js is the one missing it.
  assert.deepEqual(
    results.filter((r) => r.status === "applied").map((r) => r.id).sort(),
    ["health-hardening", "listen-loopback-hardening"],
  );
  assert.equal(text.includes("bootAccess"), false);
  assert.equal(text.includes("scope.assertCcn"), false);
  fs.rmSync(dir, { recursive: true, force: true });
});
