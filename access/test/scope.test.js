/**
 * Tests for hospital scoping.
 * Run: node --test access/test/scope.test.js
 *
 * The bug these exist for is not exotic. Every scorecard route takes the
 * hospital as `?ccn=`, so a signed-in user at Pratt Regional reads their
 * competitor by editing six digits in the address bar. Authentication is not
 * the missing piece; every one of those requests is from a real, approved,
 * MFA'd account.
 *
 * So the assertions worth writing are the denials, and the negative property on
 * the cross-hospital routes: whatever the cohort looks like, the response
 * carries no peer's name and no peer's number.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");

const { createScope, orgsFrom, rowsOf, normalizeCcn } = require(
  path.join(__dirname, "..", "scope.js"),
);

const ORGS = {
  _comment: "ignored",
  pratt: { name: "Pratt Regional Medical Center", ccns: ["170027"] },
  stfrancis: { name: "Via Christi St Francis", ccns: ["170045", "170018"] },
};

const at = (org, role = "org_member") => ({
  minerva: { email: `cfo@${org}.org`, org, role },
});

const PRATT = at("pratt");
const FRANCIS = at("stfrancis");
const ADMIN = { minerva: { email: "jeff@minervaai.health", org: "*", role: "minerva_admin" } };

// --- reading the roster ---------------------------------------------------

test("an org sees the hospitals it bought and no others", () => {
  const scope = createScope(ORGS);

  assert.equal(scope.allows(PRATT, "170027"), true);
  assert.equal(scope.allows(PRATT, "170045"), false);
  assert.equal(scope.allows(FRANCIS, "170018"), true);
});

test("minerva_admin crosses organizations and everyone else does not", () => {
  const scope = createScope(ORGS);

  assert.equal(scope.ccnsFor(ADMIN), null);
  assert.equal(scope.allows(ADMIN, "170045"), true);
  assert.equal(scope.allows(ADMIN, "999999"), true);
});

test("an org with no entry in the roster sees nothing, not everything", () => {
  // The direction the config mistake has to point. A missing line is a support
  // ticket; the other way round it is a breach.
  const scope = createScope(ORGS);
  const newCustomer = at("hutchinson");

  assert.deepEqual([...scope.ccnsFor(newCustomer)], []);
  assert.equal(scope.allows(newCustomer, "170027"), false);
});

test("a ccn is matched however it was typed", () => {
  const scope = createScope({ pratt: { ccns: ["17t027"] } });
  assert.equal(scope.allows(PRATT, " 17T027 "), true);
  assert.equal(normalizeCcn(" 170027 "), "170027");
});

// --- the denial -----------------------------------------------------------

test("asking for another hospital is refused", () => {
  const scope = createScope(ORGS);
  assert.throws(() => scope.assertCcn(PRATT, "170045"), /not in your organization/);
});

test("a refusal does not say whether the hospital exists", () => {
  // "Not yours" and "no such hospital" are different facts. Telling them apart
  // turns this endpoint into a list of who is a customer.
  const scope = createScope(ORGS);
  const real = (() => { try { scope.assertCcn(PRATT, "170045"); } catch (e) { return e; } })();
  const fake = (() => { try { scope.assertCcn(PRATT, "999999"); } catch (e) { return e; } })();

  assert.equal(real.message, fake.message);
  assert.equal(real.code, fake.code);
  assert.equal(real.status, 403);
});

test("a missing ccn is a bad request rather than a denial", () => {
  const scope = createScope(ORGS);
  assert.throws(() => scope.assertCcn(PRATT, ""), /ccn is required/);
});

test("an unauthenticated request is refused, not defaulted", () => {
  const scope = createScope(ORGS);
  assert.throws(() => scope.ccnsFor({}), /accessGuard must run/);
  assert.throws(() => scope.assertCcn(null, "170027"), /accessGuard must run/);
});

test("assertCcn returns the normalized ccn so the route can use it", () => {
  const scope = createScope(ORGS);
  assert.equal(scope.assertCcn(PRATT, " 170027 "), "170027");
});

// --- the picker -----------------------------------------------------------

test("the hospital list shows a customer only their own", () => {
  const scope = createScope(ORGS);
  const all = [
    { ccn: "170027", name: "PRATT REGIONAL" },
    { ccn: "170045", name: "VIA CHRISTI ST FRANCIS" },
    { ccn: "170001", name: "SOMEONE ELSE" },
  ];

  assert.deepEqual(scope.filter(PRATT, all).map((h) => h.ccn), ["170027"]);
  assert.equal(scope.filter(ADMIN, all).length, 3);
});

// --- the config -----------------------------------------------------------

test("the same hospital sold to two organizations fails at startup", () => {
  assert.throws(
    () => orgsFrom({ a: { ccns: ["170027"] }, b: { ccns: ["170027"] } }),
    /listed under both/,
  );
});

test("a malformed ccn fails at startup rather than silently never matching", () => {
  assert.throws(() => orgsFrom({ a: { ccns: ["17-0027"] } }), /malformed CCN/);
  assert.throws(() => orgsFrom({ a: { ccns: ["17002"] } }), /malformed CCN/);
});

test("a ccn written as a number is rejected before it loses its leading zero", () => {
  // 070027 in JSON is 70027, which matches nothing. The customer sees an empty
  // picker and no error is raised anywhere.
  assert.throws(() => orgsFrom({ a: { ccns: [170027] } }), /non-string CCN/);
});

test("an org missing its ccns list is rejected", () => {
  assert.throws(() => orgsFrom({ a: { name: "A" } }), /needs a ccns array/);
});

test("the cross-org wildcard is not a customer", () => {
  assert.throws(() => orgsFrom({ "*": { ccns: ["170027"] } }), /must not define/);
});

test("comment keys are skipped", () => {
  const scope = createScope({ _note: "hello", pratt: { ccns: ["170027"] } });
  assert.equal(scope.size, 1);
});

// --- cross-hospital results -----------------------------------------------

const RANKING = {
  state: "KS",
  count: 12,
  hospitals: [
    { ccn: "170001", name: "A", score: 10, rank: 12 },
    { ccn: "170002", name: "B", score: 20, rank: 11 },
    { ccn: "170003", name: "C", score: 30, rank: 10 },
    { ccn: "170004", name: "D", score: 40, rank: 9 },
    { ccn: "170005", name: "E", score: 50, rank: 8 },
    { ccn: "170006", name: "F", score: 60, rank: 7 },
    { ccn: "170027", name: "PRATT REGIONAL", score: 65, rank: 6 },
    { ccn: "170007", name: "G", score: 70, rank: 5 },
    { ccn: "170008", name: "H", score: 80, rank: 4 },
    { ccn: "170009", name: "I", score: 90, rank: 3 },
    { ccn: "170010", name: "J", score: 95, rank: 2 },
    { ccn: "170011", name: "K", score: 99, rank: 1 },
  ],
};

test("a state ranking comes back as your position, never as the roster", () => {
  const scope = createScope(ORGS);
  const out = scope.summarize(PRATT, RANKING, { cohortLabel: "Kansas hospitals" });

  assert.equal(out.restricted, true);
  assert.equal(out.cohortSize, 12);
  assert.equal(out.yours.length, 1);
  assert.equal(out.yours[0].name, "PRATT REGIONAL");
  assert.equal(out.yours[0].rank, 6);
  assert.equal(out.yours[0].rankOf, 12);
  assert.equal(typeof out.yours[0].percentileRank, "number");
});

test("no peer's name or number survives the summary", () => {
  // The property that matters, asserted on the serialized response so a field
  // added later cannot smuggle one through.
  const scope = createScope(ORGS);
  const json = JSON.stringify(scope.summarize(PRATT, RANKING));

  for (const peer of RANKING.hospitals) {
    if (peer.ccn === "170027") continue;
    assert.equal(json.includes(peer.ccn), false, `leaked ccn ${peer.ccn}`);
    assert.equal(json.includes(`"${peer.name}"`), false, `leaked name ${peer.name}`);
  }
  assert.equal(json.includes('"hospitals"'), false);
});

test("neither extreme is published, because each one is a real hospital", () => {
  const scope = createScope(ORGS);
  const out = scope.summarize(PRATT, RANKING);
  const you = out.yours[0];

  assert.equal("min" in you, false);
  assert.equal("max" in you, false);
  assert.equal(you.band.p25 > 10, true);
  assert.equal(you.band.p75 < 99, true);
});

test("a thin cohort gets a rank and no band", () => {
  const scope = createScope(ORGS);
  const thin = {
    hospitals: [
      { ccn: "170027", name: "PRATT REGIONAL", score: 50 },
      { ccn: "170001", name: "A", score: 10 },
      { ccn: "170002", name: "B", score: 20 },
      { ccn: "170003", name: "C", score: 30 },
      { ccn: "170004", name: "D", score: 40 },
      { ccn: "170005", name: "E", score: 60 },
    ],
  };

  const you = scope.summarize(PRATT, thin).yours[0];
  assert.equal(you.band, null);
  assert.equal(typeof you.percentileRank, "number");
  assert.equal(you.disclosure, "rank_only");
});

test("too few peers to compare withholds the comparison entirely", () => {
  const scope = createScope(ORGS);
  const tiny = {
    hospitals: [
      { ccn: "170027", name: "PRATT REGIONAL", score: 50 },
      { ccn: "170001", name: "A", score: 10 },
    ],
  };

  const you = scope.summarize(PRATT, tiny).yours[0];
  assert.equal(you.percentileRank, null);
  assert.equal(you.disclosure, "none");
  assert.match(you.note, /withheld/);
});

test("the internal view is not degraded by a control meant for customers", () => {
  const scope = createScope(ORGS);
  const out = scope.summarize(ADMIN, RANKING);

  assert.equal(out.restricted, false);
  assert.equal(out.hospitals.length, 12);
});

test("an org holding two hospitals gets a comparison for each", () => {
  const scope = createScope(ORGS);
  const rows = { hospitals: RANKING.hospitals.concat([
    { ccn: "170045", name: "ST FRANCIS", score: 55, rank: 7 },
    { ccn: "170018", name: "ST TERESA", score: 35, rank: 10 },
  ]) };

  const out = scope.summarize(FRANCIS, rows);
  assert.deepEqual(out.yours.map((h) => h.name), ["ST FRANCIS", "ST TERESA"]);
  assert.notEqual(out.yours[0].percentileRank, out.yours[1].percentileRank);
});

test("a customer with no hospital in the cohort gets no rows and no roster", () => {
  const scope = createScope(ORGS);
  const out = scope.summarize(at("hutchinson"), RANKING);

  assert.deepEqual(out.yours, []);
  assert.equal(JSON.stringify(out).includes("170011"), false);
});

// --- finding the rows -----------------------------------------------------

test("the row array is found without hard-coding the vault's key name", () => {
  // A vault release that renames `hospitals` must not make this fail open.
  assert.equal(rowsOf({ peers: [{ ccn: "1" }] }).key, "peers");
  assert.equal(rowsOf({ hospitals: [{ ccn: "1" }] }).key, "hospitals");
  assert.deepEqual(rowsOf([{ ccn: "1" }]).rows.length, 1);
});

test("a shape with no ccn rows is reported, not passed through", () => {
  const scope = createScope(ORGS);
  const out = scope.summarize(PRATT, { state: "KS", note: "nothing here" });

  assert.deepEqual(out.yours, []);
  assert.equal(out.restricted, true);
  assert.match(out.note, /No comparable rows/);
});
