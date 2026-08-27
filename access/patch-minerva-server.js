#!/usr/bin/env node
/**
 * Wire access control into the MinervaAI scorecard server.
 *
 *     node access/patch-minerva-server.js            # report only, changes nothing
 *     node access/patch-minerva-server.js --apply    # write it, after a backup
 *     node access/patch-minerva-server.js --revert   # put the original back
 *
 * ## Why a script and not a diff
 *
 * A unified diff matches on surrounding context, and `server.js` carries
 * mis-decoded punctuation in its comments — em dashes that arrived as `a-hat
 * euro`. Any transcription of those bytes that is off by one breaks the patch,
 * and `patch` fuzzing its way to a near-match is worse than failing. Every
 * anchor below is a line of code, ASCII only, and matched exactly. An anchor
 * that is not found stops the run before anything is written.
 *
 * ## What it changes, and why each one
 *
 * The server is correct for one user. It becomes wrong the moment a second
 * organization signs in, in two independent ways:
 *
 *   1. **Three module-level variables.** `uploadedDoc`, `lastEnvelope`, and
 *      `lastAgentId` are one copy each for the whole process. `uploadedDoc` is
 *      spliced into the prompt of whoever chats next, so one client's document
 *      becomes context in another client's conversation. `lastEnvelope` is what
 *      `/api/export` writes into a .docx. Neither needs a mistake by a user;
 *      they leak by working as written.
 *
 *   2. **Every route takes the hospital as `?ccn=`.** A verified, provisioned,
 *      correctly-roled user at Pratt Regional changes six digits and reads a
 *      competitor's margin, quality scores, 340B position, and negotiated
 *      prices. Their role is right — they may read *a* scorecard. The missing
 *      check is which one.
 *
 * And two that are not about tenancy at all: `/health` publishes the uploaded
 * document's filename to anyone who asks, and `app.listen(PORT)` binds to every
 * interface, so anyone who reaches the Mac mini on 3000 walks past Cloudflare
 * Access entirely.
 */

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const DEFAULT_SERVER = path.join(os.homedir(), "minerva-4.0", "server.js");

/**
 * Each edit carries the reason it exists, because a year from now the diff will
 * be read by someone deciding whether it is still needed.
 *
 * `done` is a string whose presence means the edit is already in place, so a
 * second run is a no-op rather than a double application.
 */
const EDITS = [
  {
    id: "require-boot",
    why: "Load the access modules.",
    find: 'const vault = require("./vault");',
    done: 'require("./access/boot")',
    replace:
      'const vault = require("./vault");\n' +
      'const { bootAccess } = require("./access/boot");',
  },

  {
    id: "per-caller-state",
    why:
      "Replace the three process-wide variables with per-caller state, and mount " +
      "the guard on /api before any route that reads them.",
    find: /let uploadedDoc = null;[^\n]*\nlet lastEnvelope = null;[^\n]*\nlet lastAgentId = DEFAULT_AGENT;/,
    done: "const tenants = access.tenants;",
    replace:
      "// ---------- Access control ----------\n" +
      "// Cloudflare Access answers \"who is this\". This answers \"whose data may they\n" +
      "// see\". The three variables that used to live here were one copy each for the\n" +
      "// whole process: one client's uploaded document became context in another\n" +
      "// client's chat, and /api/export wrote another client's scorecard into a docx.\n" +
      "const access = bootAccess({ appDir: __dirname, defaultAgent: DEFAULT_AGENT });\n" +
      "const scope = access.scope;\n" +
      "const tenants = access.tenants;\n" +
      "const requireMinervaAdmin = access.requireRole(access.ROLES.MINERVA_ADMIN);\n" +
      "app.use(\"/api\", access.guard);",
  },

  {
    id: "upload-per-caller",
    why: "An uploaded document belongs to the caller who uploaded it.",
    find: "    uploadedDoc = { name, text };",
    done: "tenants.for(req).uploadedDoc = { name, text };",
    replace: "    tenants.for(req).uploadedDoc = { name, text };",
  },

  {
    id: "clear-doc-per-caller",
    why: "Clearing a document must clear the caller's, not everyone's.",
    find: 'app.post("/api/clear-doc", (req, res) => { uploadedDoc = null;',
    done: 'app.post("/api/clear-doc", (req, res) => { tenants.for(req).uploadedDoc = null;',
    replace: 'app.post("/api/clear-doc", (req, res) => { tenants.for(req).uploadedDoc = null;',
  },

  {
    id: "hospital-picker",
    why:
      "The picker listed all 6,175 hospitals to every customer. It is also the " +
      "cheapest place to see the scoping is on: a pilot sees one row.",
    find: "  res.json({ hospitals: vault.list(), version: vault.version() });",
    done: "scope.filter(req, vault.list())",
    replace:
      "  res.json({ hospitals: scope.filter(req, vault.list()), version: vault.version(),\n" +
      "             org: scope.labelFor(req) });",
  },

  {
    id: "hospital-scope",
    why: "The opportunity map for any CCN the caller cares to type.",
    find: 'app.get("/api/hospital", (req, res) => {\n  const h = vault.get(req.query.ccn);',
    done: 'app.get("/api/hospital", (req, res) => {\n  scope.assertCcn',
    replace:
      'app.get("/api/hospital", (req, res) => {\n' +
      "  scope.assertCcn(req, req.query.ccn);\n" +
      "  const h = vault.get(req.query.ccn);",
  },

  {
    id: "benchmark-scope",
    why: "The subject of a benchmark has to be a hospital the caller owns.",
    find: 'app.get("/api/benchmark", (req, res) => {\n  const b = vault.benchmark(req.query.ccn);',
    done: 'app.get("/api/benchmark", (req, res) => {\n  scope.assertCcn',
    replace:
      'app.get("/api/benchmark", (req, res) => {\n' +
      "  scope.assertCcn(req, req.query.ccn);\n" +
      "  const b = vault.benchmark(req.query.ccn);",
  },

  {
    id: "scorecard-scope",
    why:
      "The whole product in one response: financials, quality, 340B, cost-to-charge, " +
      "negotiated prices. This is the route the pilot turns on and the one that " +
      "must not answer for somebody else's CCN.",
    find: 'app.get("/api/scorecard", (req, res) => {\n  const h = vault.get(req.query.ccn);',
    done: 'app.get("/api/scorecard", (req, res) => {\n  scope.assertCcn',
    replace:
      'app.get("/api/scorecard", (req, res) => {\n' +
      "  scope.assertCcn(req, req.query.ccn);\n" +
      "  const h = vault.get(req.query.ccn);",
  },

  {
    id: "peers-subject-scope",
    why: "Ask for peers of a hospital you hold, not of one you are curious about.",
    find: 'app.get("/api/peers", (req, res) => {\n  const h = vault.get(req.query.ccn);',
    done: 'app.get("/api/peers", (req, res) => {\n  scope.assertCcn',
    replace:
      'app.get("/api/peers", (req, res) => {\n' +
      "  scope.assertCcn(req, req.query.ccn);\n" +
      "  const h = vault.get(req.query.ccn);",
  },

  {
    id: "peers-roster",
    why:
      "The roster names the peer hospitals. A percentile rank is a fact about the " +
      "subject; a peer's value is that peer's data. Customers get the rank.",
    find:
      '  if (!roster) return res.status(404).json({ error: "no like-peer set for this hospital" });\n' +
      "  res.json(roster);",
    done: "scope.summarize(req, roster",
    replace:
      '  if (!roster) return res.status(404).json({ error: "no like-peer set for this hospital" });\n' +
      "  res.json(scope.summarize(req, roster, {\n" +
      '    valueKey: metric || "score",\n' +
      '    cohortLabel: roster.label || "like peers",\n' +
      "  }));",
  },

  {
    id: "state-ranking",
    why:
      "A named, scored list of every hospital in the state is a competitive " +
      "intelligence file on 120 organizations, and no customer bought it. " +
      "\"You rank 31st of 120\" is what they did buy.",
    find:
      "  res.json({ state, vintage: vault.finVintage(), count: rows.length,\n" +
      "    yearSpan: yrs.length ? { min: yrs[0], max: yrs[yrs.length - 1] } : null, hospitals: rows });",
    done: "scope.summarize(req, payload",
    replace:
      "  const payload = { state, vintage: vault.finVintage(), count: rows.length,\n" +
      "    yearSpan: yrs.length ? { min: yrs[0], max: yrs[yrs.length - 1] } : null, hospitals: rows };\n" +
      "  res.json(Object.assign(\n" +
      "    { state, vintage: payload.vintage, yearSpan: payload.yearSpan },\n" +
      "    scope.summarize(req, payload, {\n" +
      '      valueKey: "score", cohortLabel: state + " hospitals", higherIsBetter: true,\n' +
      "    })));",
  },

  {
    id: "decisions-read",
    why:
      "With no ccn this returned every decision ever recorded, for every " +
      "organization, carrying the hospital name and the officer's name.",
    find:
      "  const all = loadDecisions();\n" +
      "  const ccn = req.query.ccn;\n" +
      '  res.json({ decisions: ccn ? all.filter(d => String(d.ccn) === String(ccn)) : all });',
    done: "scope.filter(req, all)",
    replace:
      "  const all = loadDecisions();\n" +
      "  const ccn = req.query.ccn;\n" +
      "  if (ccn) scope.assertCcn(req, ccn);\n" +
      "  res.json({ decisions: ccn\n" +
      "    ? all.filter(d => String(d.ccn) === String(ccn))\n" +
      "    : scope.filter(req, all) });",
  },

  {
    id: "decisions-write",
    why: "A decision cannot be filed against another organization's hospital.",
    find:
      '  if (!b.ccn || !b.decision) return res.status(400).json({ error: "ccn and decision are required" });',
    done: "scope.assertCcn(req, b.ccn);",
    replace:
      '  if (!b.ccn || !b.decision) return res.status(400).json({ error: "ccn and decision are required" });\n' +
      "  scope.assertCcn(req, b.ccn);",
  },

  {
    id: "decisions-verify",
    why:
      "Verify takes an id, not a ccn. A row fetched by id still has to be checked " +
      "against the caller — the ids are guessable timestamps.",
    find:
      "  const rec = all.find(d => d.id === b.id);\n" +
      '  if (!rec) return res.status(404).json({ error: "decision not found" });',
    done: "scope.assertCcn(req, rec.ccn);",
    replace:
      "  const rec = all.find(d => d.id === b.id);\n" +
      '  if (!rec) return res.status(404).json({ error: "decision not found" });\n' +
      "  scope.assertCcn(req, rec.ccn);",
  },

  {
    id: "build-prompt-doc",
    why:
      "buildPrompt closed over the process-wide uploadedDoc. Taking it as a " +
      "parameter is the whole fix: the body already refers to it by that name, so " +
      "every reference now resolves to the caller's own document.",
    find: "function buildPrompt(systemPrompt, messages, hospital) {",
    done: "function buildPrompt(systemPrompt, messages, hospital, uploadedDoc) {",
    replace: "function buildPrompt(systemPrompt, messages, hospital, uploadedDoc) {",
  },

  {
    id: "bakeoff-one-admin",
    why: "Model evaluation is an internal tool that reads any CCN it is given.",
    find: 'app.post("/api/bakeoff-one", async (req, res) => {',
    done: 'app.post("/api/bakeoff-one", requireMinervaAdmin,',
    replace: 'app.post("/api/bakeoff-one", requireMinervaAdmin, async (req, res) => {',
  },

  {
    id: "bakeoff-admin",
    why: "Same tool, streaming form, same reason.",
    find: 'app.post("/api/bakeoff", async (req, res) => {',
    done: 'app.post("/api/bakeoff", requireMinervaAdmin,',
    replace: 'app.post("/api/bakeoff", requireMinervaAdmin, async (req, res) => {',
  },

  {
    id: "chat-scope",
    why:
      "The chat route grounds the model in a hospital's verified figures. " +
      "Unscoped, an agent answers questions about a competitor with real numbers, " +
      "having passed every check at the door.",
    find:
      "    lastAgentId = agent.id;\n" +
      "    const hospital = vault.get(req.body.ccn);\n" +
      "    const useModel = resolveModel(req.body.model);\n" +
      "    const prompt = buildPrompt(agent.prompt, messages, hospital);",
    done: "const st = tenants.for(req);",
    replace:
      "    const st = tenants.for(req);\n" +
      "    st.lastAgentId = agent.id;\n" +
      "    if (req.body.ccn) scope.assertCcn(req, req.body.ccn);\n" +
      "    const hospital = vault.get(req.body.ccn);\n" +
      "    const useModel = resolveModel(req.body.model);\n" +
      "    const prompt = buildPrompt(agent.prompt, messages, hospital, st.uploadedDoc);",
  },

  {
    id: "chat-envelope",
    why: "The scorecard kept for export belongs to the caller who generated it.",
    find: '      if (envelope.response_type === "scorecard") lastEnvelope = envelope;',
    done: 'if (envelope.response_type === "scorecard") st.lastEnvelope = envelope;',
    replace: '      if (envelope.response_type === "scorecard") st.lastEnvelope = envelope;',
  },

  {
    id: "chat-doc-name",
    why: "Report the caller's document name, not whoever uploaded last.",
    find: "doc: uploadedDoc ? uploadedDoc.name : null });",
    done: "doc: st.uploadedDoc ? st.uploadedDoc.name : null });",
    replace: "doc: st.uploadedDoc ? st.uploadedDoc.name : null });",
    count: 2,
  },

  {
    id: "export-per-caller",
    why:
      "Three locals shadow what used to be module scope, so the rest of the " +
      "export body needs no change: the memo is built from the caller's own " +
      "document and their own last scorecard.",
    find:
      "    const agent = agentById[req.body.agentId] || agentById[lastAgentId] || agentById[DEFAULT_AGENT];",
    done: "const uploadedDoc = st.uploadedDoc;",
    replace:
      "    const st = tenants.for(req);\n" +
      "    const uploadedDoc = st.uploadedDoc;\n" +
      "    const lastEnvelope = st.lastEnvelope;\n" +
      "    const agent = agentById[req.body.agentId] || agentById[st.lastAgentId] || agentById[DEFAULT_AGENT];",
  },

  {
    id: "health",
    groups: ["access"],
    why:
      "/health is unauthenticated so a monitor does not need a token, which is " +
      "why it must not report anything about a caller. It published the filename " +
      "of the last uploaded document to anyone who asked.",
    find: /app\.get\("\/health", \(req, res\) => res\.json\(\{[\s\S]*?\}\)\);/,
    done: "callers: tenants.stats().callers",
    replace:
      'app.get("/health", (req, res) => res.json({\n' +
      '  status: "ok", version: "4.0.6", model: effectiveDefault(), models: AVAILABLE_MODELS.length,\n' +
      "  agents: AGENTS.length, access: access.mode, callers: tenants.stats().callers\n" +
      "}));",
  },

  {
    id: "health-hardening",
    groups: ["hardening"],
    why:
      "Same leak, without the access modules: /health is unauthenticated and " +
      "reported the last uploaded document's filename to anyone who asked.",
    find: /app\.get\("\/health", \(req, res\) => res\.json\(\{[\s\S]*?\}\)\);/,
    done: "agents: AGENTS.length\n}));",
    replace:
      'app.get("/health", (req, res) => res.json({\n' +
      '  status: "ok", version: "4.0.6", model: effectiveDefault(), models: AVAILABLE_MODELS.length,\n' +
      "  agents: AGENTS.length\n" +
      "}));",
  },

  {
    id: "export-ollama-generate",
    groups: ["access", "hardening"],
    why:
      "NOT an access-control fix, and off unless --fix-export is passed: " +
      "/api/export calls ollamaGenerate(), which nothing defines, so every " +
      "Export click returns \"Export failed: ollamaGenerate is not defined\".",
    optional: "--fix-export",
    // Never add a second definition. If some copy of this file defines it
    // further down, redefining it here shadows working code with a guess.
    skipIf: (text) =>
      /(?:function\s+ollamaGenerate\b|(?:const|let|var)\s+ollamaGenerate\s*=)/.test(text),
    find: 'app.post("/api/export", async (req, res) => {',
    done: "async function ollamaGenerate(prompt, useSchema, model) {",
    replace:
      "// The non-streaming sibling of ollamaStream, which /api/export has always\n" +
      "// called and this file has never defined. Same arguments, no callbacks:\n" +
      "// export wants the finished text, not deltas.\n" +
      "async function ollamaGenerate(prompt, useSchema, model) {\n" +
      "  return ollamaStream(prompt, useSchema, null, null, model);\n" +
      "}\n" +
      "\n" +
      'app.post("/api/export", async (req, res) => {',
  },

  {
    id: "listen-loopback",
    groups: ["access", "hardening"],
    why:
      "Access protects the hostname, not the port. Bound to every interface, " +
      "anyone who reaches the Mac mini on 3000 walks past Cloudflare and past " +
      "all of the above with it. The tunnel connects to loopback, so this is free.",
    find: /const PORT = process\.env\.PORT \|\| 3000;\napp\.listen\(PORT, \(\) => console\.log\(/,
    done: 'const HOST = process.env.MINERVA_HOST || "127.0.0.1";',
    replace:
      "// A denial thrown inside a route lands here. Without it an AccessError\n" +
      "// becomes a 500, which reads as \"the server is broken\" in a log where it\n" +
      "// should read \"someone asked for a hospital that is not theirs\".\n" +
      "app.use(access.accessErrorHandler);\n" +
      "\n" +
      "const PORT = process.env.PORT || 3000;\n" +
      'const HOST = process.env.MINERVA_HOST || "127.0.0.1";\n' +
      "app.listen(PORT, HOST, () => console.log(",
  },
];

// --- applying -------------------------------------------------------------

function countOccurrences(text, find) {
  if (find instanceof RegExp) {
    const all = new RegExp(find.source, find.flags.includes("g") ? find.flags : find.flags + "g");
    return (text.match(all) || []).length;
  }
  let n = 0;
  let at = text.indexOf(find);
  while (at !== -1) {
    n += 1;
    at = text.indexOf(find, at + find.length);
  }
  return n;
}

function applyOne(text, edit, opts = {}) {
  const expected = edit.count || 1;

  // Order matters. "You did not ask for this" and "this file does not need it"
  // are both true before "it is already applied" can be, and reporting the
  // wrong one of the three sends someone looking for a problem that is not
  // there.
  const groups = edit.groups || ["access"];
  if (!groups.includes(opts.mode || "access")) {
    return { status: "skip", text };
  }
  if (edit.optional && !(opts.enable || []).includes(edit.optional)) {
    return { status: "off", text };
  }
  if (typeof edit.skipIf === "function" && edit.skipIf(text)) {
    return { status: "unneeded", text };
  }
  if (edit.done && text.includes(edit.done)) {
    return { status: "already", text };
  }

  const found = countOccurrences(text, edit.find);
  if (found === 0) return { status: "missing", text };
  if (found !== expected) return { status: "ambiguous", found, expected, text };

  let next;
  if (edit.find instanceof RegExp) {
    const flags = edit.find.flags.includes("g") ? edit.find.flags : edit.find.flags + "g";
    next = text.replace(new RegExp(edit.find.source, flags), () => edit.replace);
  } else {
    next = text.split(edit.find).join(edit.replace);
  }
  return { status: "applied", text: next };
}

function applyAll(source, opts = {}) {
  let text = source;
  const results = [];
  for (const edit of EDITS) {
    const out = applyOne(text, edit, opts);
    text = out.text;
    results.push({ id: edit.id, why: edit.why, status: out.status, found: out.found, expected: out.expected });
  }
  return { text, results };
}

/**
 * `/api/export` calls `ollamaGenerate`, which no other line in the file defines.
 *
 * Not an access-control problem, and not fixed here — adding a second
 * definition of a function that might exist further down someone else's copy is
 * how a patch breaks a working system. Reported so it is not discovered by a
 * customer clicking Export.
 */
function checkOllamaGenerate(text) {
  const called = /\bollamaGenerate\s*\(/.test(text);
  const defined = /(?:function\s+ollamaGenerate\b|(?:const|let|var)\s+ollamaGenerate\s*=)/.test(text);
  return { called, defined };
}

/**
 * The file as it was before this script first touched it.
 *
 * Every --apply writes a timestamped backup, so after a few rounds there is a
 * stack of them and only the oldest is the original: the others are snapshots
 * of an already-patched file. Reverting to the newest would look like it
 * worked and change nothing.
 */
function originalBackup(serverPath) {
  const dir = path.dirname(serverPath);
  const prefix = path.basename(serverPath) + ".bak-";
  let entries;
  try {
    entries = fs.readdirSync(dir).filter((f) => f.startsWith(prefix));
  } catch {
    return null;
  }
  if (!entries.length) return null;
  // The suffix is an ISO timestamp, so lexical order is chronological.
  entries.sort();
  return path.join(dir, entries[0]);
}

function revert(serverPath) {
  const backup = originalBackup(serverPath);
  if (!backup) {
    console.error(`No backup found beside ${serverPath}. Nothing to revert to.`);
    return 1;
  }
  const current = fs.readFileSync(serverPath, "utf8");
  const restored = fs.readFileSync(backup, "utf8");
  if (current === restored) {
    console.log(`Already matches ${path.basename(backup)}. Nothing to do.`);
    return 0;
  }
  fs.writeFileSync(serverPath, restored);
  console.log(`Restored ${serverPath}`);
  console.log(`  from ${path.basename(backup)}, the oldest backup beside it.`);
  console.log("");
  console.log("The access modules are still in access/ and are simply not called.");
  console.log("Restart to pick this up:  pm2 restart minerva-40");
  return 0;
}

// --- cli ------------------------------------------------------------------

function main(argv) {
  const apply = argv.includes("--apply");
  const at = argv.indexOf("--server");
  const serverPath = at !== -1 && argv[at + 1] ? argv[at + 1] : DEFAULT_SERVER;

  if (!fs.existsSync(serverPath)) {
    console.error(`No server file at ${serverPath}`);
    console.error("Pass the path with --server /path/to/server.js");
    return 1;
  }

  const enable = argv.filter((a) => a.startsWith("--fix-"));
  const mode = argv.includes("--hardening-only") ? "hardening" : "access";
  if (argv.includes("--revert")) return revert(serverPath);

  const source = fs.readFileSync(serverPath, "utf8");
  const { text, results } = applyAll(source, { enable, mode });

  if (mode === "hardening") {
    console.log("Hardening only. Cloudflare Access decides who gets in; nothing here");
    console.log("decides which hospital they see once they are in.");
    console.log("");
  }

  const shown = results.filter((r) => r.status !== "skip");
  const width = Math.max(...shown.map((r) => r.id.length));
  let missing = 0;
  let changed = 0;
  for (const r of results.filter((r) => r.status !== "skip")) {
    const mark = {
      applied: "  ok  ", already: " done ", missing: " MISS ",
      ambiguous: " AMBIG", off: " off  ", unneeded: " n/a  ", skip: "      ",
    }[r.status];
    console.log(`[${mark}] ${r.id.padEnd(width)}  ${r.why}`);
    if (r.status === "missing") missing += 1;
    if (r.status === "ambiguous") {
      missing += 1;
      console.log(`${" ".repeat(width + 11)}expected ${r.expected} occurrence(s), found ${r.found}`);
    }
    if (r.status === "applied") changed += 1;
  }

  const off = results.filter((r) => r.status === "off");
  if (off.length) {
    const gen = checkOllamaGenerate(source);
    console.log("");
    console.log(`${off.length} optional fix(es) are off. To include them:`);
    for (const r of off) {
      const edit = EDITS.find((e) => e.id === r.id);
      console.log(`  ${edit.optional}   ${r.id}`);
    }
    if (gen.called && !gen.defined) {
      console.log("  (confirmed on this file: ollamaGenerate is called and never defined)");
    }
  }

  console.log("");
  if (missing) {
    console.error(`${missing} anchor(s) not found. Nothing was written.`);
    console.error("The file has drifted from the version this was written against;");
    console.error("send the surrounding lines and the anchor can be updated.");
    return 1;
  }

  if (!changed) {
    console.log("Already fully patched. Nothing to do.");
    return 0;
  }

  if (!apply) {
    console.log(`${changed} edit(s) would be applied. Re-run with --apply to write them.`);
    return 0;
  }

  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const backup = `${serverPath}.bak-${stamp}`;
  fs.copyFileSync(serverPath, backup);
  fs.writeFileSync(serverPath, text);
  console.log(`Applied ${changed} edit(s).`);
  console.log(`Backup: ${backup}`);
  console.log("");
  if (mode === "hardening") {
    console.log("Nothing else to configure. Restart and you are done:");
    console.log("  pm2 restart minerva-40");
    return 0;
  }
  console.log("Before restarting, make sure these exist and are filled in:");
  console.log("  <app>/access/directory.json   who works for which organization");
  console.log("  <app>/access/orgs.json        which hospitals each organization may read");
  console.log("and that ACCESS_TEAM_DOMAIN and ACCESS_AUD are set, or the server");
  console.log("will refuse to start. That refusal is deliberate.");
  return 0;
}

if (require.main === module) {
  process.exit(main(process.argv.slice(2)));
}

module.exports = {
  EDITS, applyAll, applyOne, checkOllamaGenerate, originalBackup, revert, main,
};
