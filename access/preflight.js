#!/usr/bin/env node
/**
 * Show what each provisioned person will actually see.
 *
 *     node access/preflight.js                       # the app beside this file
 *     node access/preflight.js --app ~/minerva-4.0
 *
 * `directory.json` and `orgs.json` are two files that only mean something
 * together, and every way of getting them wrong looks fine in an editor:
 *
 *   - a user whose org has no entry in `orgs.json` signs in successfully and
 *     sees an empty hospital picker, with no error anywhere;
 *   - an org that bought hospitals but has nobody in the directory is a
 *     customer who cannot log in;
 *   - a typo'd email is not a syntax error, it is a person who is refused at
 *     the door on the morning of the demo;
 *   - and forgetting your own address locks you out of your own product.
 *
 * So this runs both files through the same code the server does, then prints
 * the answer the config is really giving. A wrong roster is easy to see when
 * it says "sees nothing" next to somebody's name, and nearly invisible in JSON.
 *
 * Run it before `pm2 restart`, not after.
 */

"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { directoryFrom, ROLES } = require("./access-control");
const { createScope } = require("./scope");

/** Everyone the directory names, ignoring the `_comment` convention. */
function emailsIn(directory) {
  return Object.keys(directory).filter((k) => !k.startsWith("_"));
}

/**
 * What one person sees, and whether that is what anyone intended.
 *
 * `sees` is deliberately the same string a person would say out loud —
 * "1 hospital: 170027", "every hospital" — because the failure this catches is
 * someone reading `{"org": "pratt"}` and assuming it works.
 */
function describe(email, identity, scope) {
  const req = { minerva: { email, org: identity.org, role: identity.role } };
  const ccns = scope.ccnsFor(req);

  if (ccns === null) {
    return { email, org: identity.org, role: identity.role, sees: "every hospital", problem: null };
  }
  if (ccns.size === 0) {
    return {
      email,
      org: identity.org,
      role: identity.role,
      sees: "NOTHING",
      problem: `org "${identity.org}" has no entry in orgs.json`,
    };
  }
  const list = [...ccns].sort();
  const shown = list.slice(0, 4).join(", ") + (list.length > 4 ? `, +${list.length - 4} more` : "");
  return {
    email,
    org: identity.org,
    role: identity.role,
    sees: `${list.length} hospital${list.length === 1 ? "" : "s"}: ${shown}`,
    problem: null,
  };
}

function review(directory, orgs) {
  const lookup = directoryFrom(directory);   // throws on a bad role or a stray "*"
  const scope = createScope(orgs);           // throws on a duplicate or malformed CCN

  const rows = emailsIn(directory).map((email) => describe(email, lookup(email), scope));

  const problems = rows.filter((r) => r.problem).map((r) => `${r.email}: ${r.problem}`);

  // An org that bought hospitals and has nobody who can log in.
  const staffed = new Set(rows.map((r) => r.org));
  for (const orgId of Object.keys(orgs)) {
    if (orgId.startsWith("_")) continue;
    if (!staffed.has(orgId)) {
      problems.push(`org "${orgId}" is in orgs.json but nobody in directory.json belongs to it`);
    }
  }

  // Nobody who can see across organizations is a locked door with you outside.
  if (!rows.some((r) => r.role === ROLES.MINERVA_ADMIN)) {
    problems.push(
      "no minerva_admin in directory.json — nobody can see across organizations, " +
      "including you",
    );
  }

  return { rows, problems };
}

function readJson(file, what) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (err) {
    if (err.code === "ENOENT") throw new Error(`${what} not found at ${file}`);
    throw new Error(`${what} at ${file} is not valid JSON: ${err.message}`);
  }
}

function main(argv) {
  const at = argv.indexOf("--app");
  const app = at !== -1 && argv[at + 1]
    ? argv[at + 1].replace(/^~/, os.homedir())
    : path.dirname(__dirname);
  const dir = path.join(app, "access");

  let rows;
  let problems;
  try {
    const directory = readJson(path.join(dir, "directory.json"), "directory.json");
    const orgs = readJson(path.join(dir, "orgs.json"), "orgs.json");
    ({ rows, problems } = review(directory, orgs));
  } catch (err) {
    console.error(`Config error — the server would refuse to start:\n  ${err.message}`);
    return 1;
  }

  const w = (key) => Math.max(...rows.map((r) => String(r[key]).length), key.length);
  const we = w("email");
  const wr = w("role");
  console.log(`${"email".padEnd(we)}  ${"role".padEnd(wr)}  sees`);
  console.log(`${"-".repeat(we)}  ${"-".repeat(wr)}  ----`);
  for (const r of rows) {
    console.log(`${r.email.padEnd(we)}  ${r.role.padEnd(wr)}  ${r.sees}`);
  }

  if (problems.length) {
    console.log("");
    for (const p of problems) console.log(`  PROBLEM  ${p}`);
    console.log("");
    console.log("These are not syntax errors. The server starts and each one shows up");
    console.log("as a person who cannot use the product.");
    return 1;
  }

  console.log("");
  console.log(`${rows.length} provisioned user(s), no problems found.`);
  console.log("");
  console.log("One thing this cannot check: Cloudflare Access has its own list of who");
  console.log("may reach the door. An address here that Access does not admit never");
  console.log("arrives; an address Access admits that is missing here gets 403");
  console.log("not_provisioned. After a failed sign-in, the exact address Access");
  console.log("asserted is the last line of access-audit.log.");
  return 0;
}

if (require.main === module) {
  process.exit(main(process.argv.slice(2)));
}

module.exports = { review, describe, emailsIn, main };
