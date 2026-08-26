/**
 * One call that turns the three access modules into the four things a server
 * needs: a guard, a hospital scope, per-caller state, and an error handler.
 *
 * This exists so the change to `server.js` stays small enough to read. Wiring
 * spread across twenty lines at the top of a 760-line file is wiring nobody
 * reviews, and access control that nobody reviews is decoration.
 *
 * ## It refuses to start rather than start open
 *
 * Without `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` there is no way to verify a
 * Cloudflare Access token, and the only two options are "reject everything" or
 * "trust everything". A server that will not boot is a five-minute problem with
 * an obvious cause. A server that boots and serves every hospital's data to
 * anyone who finds the port is the other kind of problem.
 *
 * There is a development path — `MINERVA_DEV_IDENTITY=cfo@prattregional.org` —
 * because refusing to run on a laptop is how a control gets commented out. It
 * announces itself on every request and refuses to load when NODE_ENV is
 * production.
 */

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const { createAccessGuard, requireRole, AccessError, ROLES } = require("./access-control");
const { createScope } = require("./scope");
const { TenantState } = require("./tenant-state");

const SWEEP_INTERVAL_MS = 15 * 60 * 1000;

function readJson(file, what) {
  let raw;
  try {
    raw = fs.readFileSync(file, "utf8");
  } catch (err) {
    if (err.code === "ENOENT") {
      throw new Error(
        `${what} not found at ${file}. Copy the example beside it and fill it in:\n` +
        `    cp ${file.replace(/\.json$/, ".example.json")} ${file}`,
      );
    }
    throw err;
  }
  try {
    return JSON.parse(raw);
  } catch (err) {
    throw new Error(`${what} at ${file} is not valid JSON: ${err.message}`);
  }
}

/**
 * Append one line per decision.
 *
 * The audit log records the decision and the identity, never the payload — a
 * log of who read which scorecard is an operational record; a log of what was
 * in it is a second copy of the data with weaker protection than the first.
 */
function fileAuditor(file) {
  return (entry) => {
    try {
      fs.appendFileSync(file, JSON.stringify(entry) + "\n");
    } catch {
      // Auditing must never be the reason a request fails.
    }
  };
}

/**
 * A guard for a laptop with no Cloudflare tunnel in front of it.
 *
 * Loud on purpose. A quiet development bypass is one `NODE_ENV` slip away from
 * being the production configuration.
 */
function devGuard(email, directory) {
  const identity = directory[String(email).trim().toLowerCase()];
  if (!identity) {
    throw new Error(
      `MINERVA_DEV_IDENTITY is ${email}, which is not in directory.json. ` +
      "Add it, or the dev server would grant an identity the real one would deny.",
    );
  }
  let warned = false;
  return function developmentGuard(req, res, next) {
    if (!warned) {
      warned = true;
      console.warn(
        "\n*** ACCESS CONTROL IS IN DEVELOPMENT MODE ***\n" +
        `    Every request is treated as ${email} (${identity.org}/${identity.role}).\n` +
        "    No token is verified. Do not expose this process to a network.\n" +
        "    Set ACCESS_TEAM_DOMAIN and ACCESS_AUD to run for real.\n",
      );
    }
    req.minerva = { email: String(email).trim().toLowerCase(), org: identity.org, role: identity.role };
    next();
  };
}

/**
 * Translate an access denial into a status code.
 *
 * Without this an `AccessError` thrown inside a route becomes a 500, which
 * reads as "the server is broken" in a log where it should read "someone asked
 * for a hospital that is not theirs".
 */
function accessErrorHandler(err, req, res, next) {
  if (res.headersSent) return next(err);
  const status = Number(err && err.status);
  if (err instanceof AccessError || (status >= 400 && status < 500 && err.code)) {
    return res.status(status || 403).json({ error: err.code, message: err.message });
  }
  return next(err);
}

function boot(options = {}) {
  const {
    appDir = process.cwd(),
    defaultAgent = null,
    env = process.env,
  } = options;

  const accessDir = path.join(appDir, "access");
  const directory = readJson(path.join(accessDir, "directory.json"), "directory.json");
  const orgs = readJson(path.join(accessDir, "orgs.json"), "orgs.json");
  const scope = createScope(orgs);

  const teamDomain = env.ACCESS_TEAM_DOMAIN;
  const aud = env.ACCESS_AUD;
  const devIdentity = env.MINERVA_DEV_IDENTITY;

  let guard;
  let mode;
  if (teamDomain && aud) {
    mode = "cloudflare-access";
    guard = createAccessGuard({
      teamDomain,
      aud,
      directory,
      onAudit: fileAuditor(path.join(appDir, "access-audit.log")),
    });
  } else if (devIdentity && env.NODE_ENV !== "production") {
    mode = "development";
    guard = devGuard(devIdentity, directory);
  } else {
    throw new Error(
      "Access control is not configured, so the server will not start.\n" +
      "  Set both of these to the values from the Cloudflare Access application:\n" +
      "    ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com\n" +
      "    ACCESS_AUD=<the application's AUD tag>\n" +
      "  Or, for local development only:\n" +
      "    MINERVA_DEV_IDENTITY=cfo@prattregional.org\n" +
      "  Refusing to run is deliberate: the alternative is serving every\n" +
      "  hospital's data to anyone who reaches the port.",
    );
  }

  const tenants = new TenantState({
    defaults: () => ({ uploadedDoc: null, lastEnvelope: null, lastAgentId: defaultAgent }),
  });
  const sweeper = setInterval(() => tenants.sweep(), SWEEP_INTERVAL_MS);
  if (typeof sweeper.unref === "function") sweeper.unref();

  console.log(
    `Access control: ${mode}; ${scope.size} organization(s) scoped, ` +
    `${Object.keys(directory).filter((k) => !k.startsWith("_")).length} provisioned user(s).`,
  );

  return { guard, scope, tenants, mode, accessErrorHandler, requireRole, ROLES };
}

module.exports = { boot, bootAccess: boot, accessErrorHandler, fileAuditor };
