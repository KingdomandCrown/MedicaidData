/**
 * Tests for the config preflight.
 * Run: node --test access/test/preflight.test.js
 *
 * Every case here is a configuration that is valid JSON, passes every schema
 * check, starts the server cleanly, and is wrong. That is the whole reason the
 * script exists — the mistakes it catches do not announce themselves.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");

const { review, describe: describeUser, emailsIn } = require(
  path.join(__dirname, "..", "preflight.js"),
);

const ADMIN = { "jeff@minervaai.health": { org: "*", role: "minerva_admin" } };

const ok = (extra = {}) => Object.assign({}, ADMIN, extra);

// --- what each person sees ------------------------------------------------

test("a customer sees the hospitals their org bought", () => {
  const { rows, problems } = review(
    ok({ "cfo@pratt.org": { org: "pratt", role: "org_admin" } }),
    { pratt: { ccns: ["170027"] } },
  );

  assert.deepEqual(problems, []);
  const cfo = rows.find((r) => r.email === "cfo@pratt.org");
  assert.equal(cfo.sees, "1 hospital: 170027");
});

test("minerva_admin sees everything", () => {
  const { rows } = review(ok(), { pratt: { ccns: ["170027"] } });
  assert.equal(rows.find((r) => r.role === "minerva_admin").sees, "every hospital");
});

test("a long roster is summarized rather than dumped", () => {
  const { rows } = review(
    ok({ "a@b.org": { org: "sys", role: "org_member" } }),
    { sys: { ccns: ["170001", "170002", "170003", "170004", "170005", "170006"] } },
  );
  assert.match(rows.find((r) => r.email === "a@b.org").sees, /\+2 more$/);
});

// --- the three configurations that look fine and are not ------------------

test("a user whose org bought nothing is reported, not left to discover it", () => {
  // Valid JSON, valid role, server starts, picker is empty, no error anywhere.
  const { rows, problems } = review(
    ok({ "cfo@hutchinson.org": { org: "hutchinson", role: "org_admin" } }),
    { pratt: { ccns: ["170027"] } },
  );

  assert.equal(rows.find((r) => r.email === "cfo@hutchinson.org").sees, "NOTHING");
  assert.equal(problems.some((p) => p.includes("hutchinson") && p.includes("orgs.json")), true);
});

test("an org with hospitals and no staff is a customer who cannot log in", () => {
  // The minerva_admin's "*" does not staff anybody's organization — being able
  // to see a customer's data is not the same as the customer being able to.
  const { problems } = review(
    ok({ "cfo@pratt.org": { org: "pratt", role: "org_admin" } }),
    { pratt: { ccns: ["170027"] }, hutchinson: { ccns: ["170004"] } },
  );

  assert.equal(problems.length, 1);
  assert.match(problems[0], /hutchinson.*nobody in directory\.json/);
});

test("no minerva_admin means you have locked yourself out of your own product", () => {
  const { problems } = review(
    { "cfo@pratt.org": { org: "pratt", role: "org_admin" } },
    { pratt: { ccns: ["170027"] } },
  );

  assert.equal(problems.some((p) => p.includes("no minerva_admin")), true);
});

test("a clean pair of files reports nothing", () => {
  const { problems } = review(
    ok({ "cfo@pratt.org": { org: "pratt", role: "org_admin" } }),
    { pratt: { ccns: ["170027"] } },
  );
  assert.deepEqual(problems, []);
});

// --- errors that stop the server, surfaced before the restart -------------

test("an unknown role is raised here rather than at boot", () => {
  assert.throws(
    () => review({ "a@b.org": { org: "pratt", role: "superuser" } }, { pratt: { ccns: ["170027"] } }),
    /unknown role/,
  );
});

test("a non-admin holding the cross-org wildcard is refused", () => {
  assert.throws(
    () => review({ "a@b.org": { org: "*", role: "org_admin" } }, {}),
    /only minerva_admin/,
  );
});

test("a hospital sold to two organizations is refused", () => {
  assert.throws(
    () => review(ok(), { a: { ccns: ["170027"] }, b: { ccns: ["170027"] } }),
    /listed under both/,
  );
});

// --- housekeeping ---------------------------------------------------------

test("comment keys are not people", () => {
  assert.deepEqual(emailsIn({ _comment: ["hi"], "a@b.org": {} }), ["a@b.org"]);
});

test("one hospital is singular and two are plural", () => {
  const scope = require(path.join(__dirname, "..", "scope.js")).createScope({
    one: { ccns: ["170027"] },
    two: { ccns: ["170027".replace("7", "8"), "170045"] },
  });

  assert.match(describeUser("a@b.org", { org: "one", role: "org_member" }, scope).sees, /1 hospital:/);
  assert.match(describeUser("a@b.org", { org: "two", role: "org_member" }, scope).sees, /2 hospitals:/);
});
