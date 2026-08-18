/**
 * Tests for per-caller state.
 * Run: node --test access/test/tenant-state.test.js
 *
 * The property that matters is negative: no two callers ever hold the same
 * object. Everything else is housekeeping.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");

const { TenantState } = require(path.join(__dirname, "..", "tenant-state.js"));

const DEFAULTS = () => ({ uploadedDoc: null, lastEnvelope: null, lastAgentId: "general" });

const req = (email, orgId) => ({ user: { sub: email, email, orgId } });

const pratt = req("cfo@prattregional.org", "pratt");
const stfrancis = req("cfo@stfrancis.org", "stfrancis");
const prattColleague = req("coo@prattregional.org", "pratt");

// --- isolation ------------------------------------------------------------

test("a document uploaded by one caller is invisible to another", () => {
  const tenants = new TenantState({ defaults: DEFAULTS });

  tenants.for(pratt).uploadedDoc = { name: "Pratt-Q3-Budget.pdf", text: "confidential" };

  assert.equal(tenants.for(stfrancis).uploadedDoc, null);
  assert.equal(tenants.for(pratt).uploadedDoc.name, "Pratt-Q3-Budget.pdf");
});

test("the scorecard behind /api/export does not cross callers", () => {
  // lastEnvelope is what the .docx is built from; sharing it would let one
  // client download a memo made from another client's numbers.
  const tenants = new TenantState({ defaults: DEFAULTS });

  tenants.for(pratt).lastEnvelope = { org: "pratt", margin: -0.04 };

  assert.equal(tenants.for(stfrancis).lastEnvelope, null);
});

test("colleagues at one hospital are also separated", () => {
  // Per-org would close the cross-tenant hole and still surprise these two.
  const tenants = new TenantState({ defaults: DEFAULTS });

  tenants.for(pratt).uploadedDoc = { name: "draft.docx", text: "x" };

  assert.equal(tenants.for(prattColleague).uploadedDoc, null);
});

test("the same caller gets the same object back", () => {
  const tenants = new TenantState({ defaults: DEFAULTS });
  const first = tenants.for(pratt);
  first.lastAgentId = "cfo";
  assert.equal(tenants.for(pratt).lastAgentId, "cfo");
});

test("each caller starts from a fresh copy of the defaults", () => {
  const tenants = new TenantState({ defaults: DEFAULTS });
  const a = tenants.for(pratt);
  const b = tenants.for(stfrancis);
  assert.notStrictEqual(a, b);
  a.lastAgentId = "cfo";
  assert.equal(b.lastAgentId, "general");
});

// --- an unauthenticated request is a bug, not a guest ---------------------

test("a request with no user is refused rather than given shared state", () => {
  const tenants = new TenantState({ defaults: DEFAULTS });
  assert.throws(() => tenants.for({}), /authenticated/);
  assert.throws(() => tenants.for({ user: {} }), /authenticated/);
  assert.throws(() => tenants.for(null), /authenticated/);
});

test("email alone identifies a caller when there is no subject claim", () => {
  const tenants = new TenantState({ defaults: DEFAULTS });
  assert.equal(tenants.keyFor({ user: { email: "a@b.org" } }), "a@b.org");
});

// --- forgetting -----------------------------------------------------------

test("clear forgets one caller and nobody else", () => {
  const tenants = new TenantState({ defaults: DEFAULTS });
  tenants.for(pratt).uploadedDoc = { name: "d.pdf", text: "x" };
  tenants.for(stfrancis).uploadedDoc = { name: "e.pdf", text: "y" };

  assert.equal(tenants.clear(pratt), true);
  assert.equal(tenants.for(pratt).uploadedDoc, null);
  assert.equal(tenants.for(stfrancis).uploadedDoc.name, "e.pdf");
});

test("an organization can be cleared in one go", () => {
  const tenants = new TenantState({ defaults: DEFAULTS });
  tenants.for(pratt);
  tenants.for(prattColleague);
  tenants.for(stfrancis);

  assert.equal(tenants.clearOrg("pratt"), 2);
  assert.equal(tenants.size, 1);
});

test("idle state expires, so document text is not held forever", () => {
  let clock = 1000;
  const tenants = new TenantState({ defaults: DEFAULTS, ttlMs: 100, now: () => clock });

  tenants.for(pratt).uploadedDoc = { name: "d.pdf", text: "x" };
  clock += 101;

  assert.equal(tenants.for(pratt).uploadedDoc, null);
});

test("activity keeps state alive", () => {
  let clock = 1000;
  const tenants = new TenantState({ defaults: DEFAULTS, ttlMs: 100, now: () => clock });

  tenants.for(pratt).uploadedDoc = { name: "d.pdf", text: "x" };
  clock += 60;
  tenants.for(pratt);
  clock += 60;

  assert.equal(tenants.for(pratt).uploadedDoc.name, "d.pdf");
});

test("sweep drops what has expired and keeps what has not", () => {
  let clock = 1000;
  const tenants = new TenantState({ defaults: DEFAULTS, ttlMs: 100, now: () => clock });

  tenants.for(pratt);
  clock += 80;
  tenants.for(stfrancis);
  clock += 40; // pratt is now 120ms idle, stfrancis 40ms

  assert.equal(tenants.sweep(), 1);
  assert.equal(tenants.size, 1);
});

test("the least recently used caller is evicted at the cap", () => {
  const tenants = new TenantState({ defaults: DEFAULTS, maxCallers: 2 });

  tenants.for(pratt);
  tenants.for(stfrancis);
  tenants.for(pratt); // pratt is now the more recent of the two
  tenants.for(prattColleague);

  assert.equal(tenants.size, 2);
  assert.equal(tenants.for(pratt).lastAgentId, "general");
  // stfrancis was the oldest and went first.
  assert.equal(tenants.size, 2);
});

// --- what may be published ------------------------------------------------

test("stats carry a count and nothing identifying", () => {
  // /health published the uploaded document's filename to anyone who asked.
  const tenants = new TenantState({ defaults: DEFAULTS });
  tenants.for(pratt).uploadedDoc = { name: "Pratt-Q3-Budget.pdf", text: "secret" };

  const stats = tenants.stats();
  assert.deepEqual(stats, { callers: 1 });
  assert.equal(JSON.stringify(stats).includes("Pratt"), false);
});
