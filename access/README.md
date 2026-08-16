# MinervaAI access control

Cloudflare Access answers **who is this** — approved email plus MFA. It does not
answer **whose data may they see**, which is the question a pilot with a named
hospital turns on. This module supplies the second half.

```
Cloudflare Access  ──►  accessGuard  ──►  your routes
   authentication       authorization      org-scoped queries
   (email + MFA)        (org + role)
```

## Wiring it up

```js
const express = require("express");
const {
  createAccessGuard, requireRole, orgFilter, assertOrg, ROLES,
} = require("./access/access-control");

const app = express();

app.use(createAccessGuard({
  teamDomain: "minervaai.cloudflareaccess.com",  // your Access team domain
  aud: process.env.ACCESS_AUD,                   // the application's AUD tag
  directory: require("./access/directory.json"), // email -> { org, role }
  onAudit: (entry) => auditLog.write(entry),     // optional but do it
}));

// Every query filters by the org on the verified session — never by anything
// the client sent.
app.get("/api/scorecard", async (req, res) => {
  res.json(await db.scorecard({ org: orgFilter(req) }));
});

// A row fetched by id still has to be checked against the caller.
app.get("/api/report/:id", async (req, res) => {
  const report = await db.report(req.params.id);
  assertOrg(req, report.org);          // throws unless it is theirs
  res.json(report);
});

app.post("/api/users", requireRole(ROLES.ORG_ADMIN), createUser);
```

`req.minerva` is `{ email, org, role }`. Read the org from there and nowhere
else — a query string or request body is client-controlled.

## Lock the origin, or none of this holds

Access protects the *hostname*, not the *port*. If minerva is reachable
directly — `http://<mac-mini-ip>:3000`, a LAN address, an old port-forward —
anyone who finds it walks straight past Access, and past this module with it.

This guard rejects a request that carries no valid Access assertion, so a direct
hit fails closed. That is the backstop, not the plan. Bind the app to
`127.0.0.1` and let the Cloudflare tunnel be the only route in:

```js
app.listen(3000, "127.0.0.1");
```

Then confirm from another machine that the port is not answering.

Related: the guard never trusts `Cf-Access-Authenticated-User-Email`. That
header is a plain string any direct client can set. The signed
`Cf-Access-Jwt-Assertion` is verified against Cloudflare's public keys instead,
with the issuer and the application's **AUD tag** both checked — without the AUD
check, a token minted for a *different* Access app in the same team would be
accepted.

## Roles

| Role | Can |
|---|---|
| `org_viewer` | read their own scorecard |
| `org_member` | + use the coaches and agents |
| `org_admin` | + manage their own organization's users |
| `minerva_admin` | cross organizations — the only role that may |

Roles are ranked, so `requireRole(ROLES.ORG_MEMBER)` also admits admins.

## Agents are the leak path to watch

Endpoint auth does not contain a model. If the retrieval behind a coach is not
org-scoped, an agent will cite another hospital's numbers from a session that
passed every check at the door. Call `orgFilter(req)` in the retrieval query and
keep other orgs' rows out of the context window entirely.

Do **not** rely on the system prompt for this. "Never reveal other hospitals'
data" is a suggestion to a model, not a control — the same distinction as the
healthcare-topic guardrails: the prompt shapes behavior, the code enforces it.

## Peer benchmarking is a deliberate exception

The CCR-vs-peers view only works by reading across organizations — that is the
feature. It stays safe while a peer cannot be picked out of the cohort, so it
needs a floor:

```js
const { peerCohortIsSafe } = require("./access/access-control");

if (!peerCohortIsSafe(peers.length)) {
  return { withheld: "too few peers to compare without identifying them" };
}
```

`MIN_PEER_COHORT` is 5, matching the Readiness module's gate on manager views.
Show percentiles against a cohort; never name a peer. With four hospitals in the
cohort, a percentile *is* a named competitor with extra steps.

## The directory

Copy `directory.example.json` to `directory.json` and keep it out of git — it is
a list of your customers' staff. An email Access admits but the directory does
not list is denied (`403 not_provisioned`) rather than given a default org.

Malformed entries fail at startup, not at request time: an unknown role, a
missing org, or a non-staff account holding org `"*"` all throw when the guard
is built.

## Tests

```bash
node --test access/test/access-control.test.js
```

31 tests, no dependencies. The interesting ones are the denials — forged
signature, `alg: none`, wrong audience, wrong team, expired, unprovisioned
email, unreachable key endpoint — since those are what protect the pilot.
