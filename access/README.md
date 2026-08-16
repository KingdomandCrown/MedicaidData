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
feature. `benchmark.js` releases it without releasing anyone's numbers, on one
distinction:

- A **percentile rank** is a fact about *the subject*. "You sit at the 30th
  percentile of Kansas CAHs" says nothing about any individual peer, at any
  cohort size.
- A **cohort statistic** is a fact about *the peers*. A median is somebody's
  data, and on a small sample it is exactly one hospital's number.

So the rank is the primary output and the quartile band is an extra, released
only when the cohort is big enough to hide an individual inside it.

```js
const { buildPeerBenchmark, assertPredefinedCohort, COHORTS } = require("./access/benchmark");

app.get("/api/benchmark/ccr", async (req, res) => {
  assertPredefinedCohort(req.query.cohort);      // never a client-composed filter
  const { subject, peers } = await db.ccrCohort({
    org: orgFilter(req),
    cohort: req.query.cohort,
  });
  res.json(buildPeerBenchmark({
    subjectValue: subject,
    peerValues: peers,                            // others only; never the subject
    cohortLabel: "Kansas critical access hospitals",
  }));
});
```

The response carries `cohortSize`, `yourValue`, `percentileRank`, and either a
`band` or a `note` explaining what was withheld — no peer list, no minimum, no
maximum. Three release levels:

| Peers | Released |
|---|---|
| < 5 | nothing — `disclosure: "none"` |
| 5–10 | rank only |
| 11+ | rank + `{ p25, p50, p75 }` |

Quartiles need the higher floor because they are order statistics: the median of
seven values *is* the fourth hospital's number. Above the floor the interpolation
positions sit well inside the cohort, so the extremes can never be published —
and the extremes are the identifiable hospitals. A uniform cohort also withholds
the band, since publishing the median of twelve identical values publishes all
twelve.

**Differencing is the trap this cannot close on its own.** Offering "Kansas CAHs"
(n=6) next to "Kansas CAHs under 25 beds" (n=5) lets anyone subtract one from the
other and isolate a single hospital. Cohorts must come from the fixed `COHORTS`
set — `assertPredefinedCohort` rejects anything else with a 400. Never build a
cohort from query-string filters.

## The directory

Copy `directory.example.json` to `directory.json` and keep it out of git — it is
a list of your customers' staff. An email Access admits but the directory does
not list is denied (`403 not_provisioned`) rather than given a default org.

Malformed entries fail at startup, not at request time: an unknown role, a
missing org, or a non-staff account holding org `"*"` all throw when the guard
is built.

## Tests

```bash
node --test "access/test/*.test.js"
```

49 tests, no dependencies. The interesting ones are the denials — forged
signature, `alg: none`, wrong audience, wrong team, expired, unprovisioned
email, unreachable key endpoint — since those are what protect the pilot. The
benchmark suite adds the negative property: whatever the cohort looks like, the
response carries no peer list and neither extreme.
