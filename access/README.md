# MinervaAI access control

Cloudflare Access answers **who is this** — approved email plus MFA. It does not
answer **whose data may they see**, which is the question a pilot with a named
hospital turns on. This module supplies the second half.

```
Cloudflare Access  ──►  accessGuard  ──►  scope  ──►  your routes
   authentication       authorization     which        per-caller state
   (email + MFA)        (org + role)      hospital
```

Four files, in the order a request meets them:

| File | Answers |
|---|---|
| `access-control.js` | Is this a real Access token, and whose is it? |
| `scope.js` | May this organization read *this hospital*? |
| `tenant-state.js` | Whose uploaded document is this? |
| `boot.js` | Assembles the three in one call |

## Installing

```bash
bash access/install-access.sh --app ~/minerva            # check only, changes nothing
bash access/install-access.sh --app ~/minerva --apply    # copy the modules in
```

The check pass reports the things that decide whether any of this holds: the
server entry point, whether the app is bound to loopback or to every interface,
and whether `ACCESS_AUD` / `ACCESS_TEAM_DOMAIN` are set. `--apply` copies the
modules, creates `directory.json` if absent, backs up any existing `access/`
directory, and writes a matching `rollback-access-<timestamp>.sh`.

It deliberately does **not** edit your server file — guessing at an unseen
server is how installers break working systems. It prints the lines to add and
where.

Rollback removes the modules and restores any backup. It leaves `directory.json`
alone: that file is your customer list, not something an install created.

## Wiring it up

For `minerva-4.0/server.js` this is scripted, because that file is known:

```bash
node access/patch-minerva-server.js            # report only, changes nothing
node access/patch-minerva-server.js --apply    # write it, after a backup
```

23 edits. It refuses to write if any anchor has moved, backs the file up first,
and is safe to run twice. `access/test/patch-minerva-server.test.js` checks that
every anchor matches, that the result still parses, and that no route taking a
`?ccn=` is left unchecked.

### Cloudflare only

If you would rather let Cloudflare Access be the whole of it — one allowlist,
one place to manage it, no `directory.json` and no `orgs.json` — that is a
coherent choice, and while there is one customer it is the right one. Access
answers *who gets in*, which is the entire question until a second organization
has a login.

```bash
node access/patch-minerva-server.js --apply --hardening-only
```

Two edits, no modules, no configuration, nothing to keep in sync:

- **`app.listen(PORT)` → `app.listen(PORT, HOST)`.** This one is not optional
  under any policy. Access protects a *hostname*; the origin port is a separate
  door. Bound to every interface, anyone who can reach the machine on that port
  skips Cloudflare entirely — so without this, "managed through Cloudflare"
  isn't true.
- **`/health` stops publishing the last uploaded document's filename** to
  anyone who asks, authenticated or not.

What you give up is the second question. Once a Pratt login exists, that user
can change six digits in `?ccn=` and read any of the other 6,174 hospitals —
their margin, quality scores, 340B position, negotiated rates. Cloudflare has
no view into that; it is one API call inside a session it already approved.

Run the full patch before the first customer signs in, not after.

One more edit is off unless asked for. `/api/export` calls `ollamaGenerate()`,
which nothing in `server.js` defines, so every Export click returns *"Export
failed: ollamaGenerate is not defined"*. That is a real bug and not an
access-control one, and a security patch carrying a silent functional change is
a patch nobody can review as either:

```bash
node access/patch-minerva-server.js --apply --fix-export
```

It adds the missing non-streaming wrapper, and skips itself entirely if a
definition turns out to exist.

For anything else, three lines near the top:

```js
const express = require("express");
const { bootAccess } = require("./access/boot");

const app = express();
const access = bootAccess({ appDir: __dirname });

app.use("/api", access.guard);
```

`bootAccess` reads `access/directory.json` and `access/orgs.json`, builds the
guard, the scope, and the per-caller state store, and **throws if Access is not
configured** — see *It refuses to start* below. Then, after the last route:

```js
app.use(access.accessErrorHandler);   // a denial is a 403, not a 500
```

If you would rather assemble it by hand:

```js
const {
  createAccessGuard, requireRole, orgFilter, assertOrg, ROLES,
} = require("./access/access-control");

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

## The hospital is in the query string

Authentication is not the whole problem, and on this app it is not even the
interesting half. Every scorecard route takes the hospital as a parameter:

```
GET /api/scorecard?ccn=170027
```

A verified, provisioned, correctly-roled user at Pratt Regional changes six
digits and reads a competitor's margin, quality scores, 340B position, and
negotiated prices. Their role is *right* — they are allowed to read a scorecard.
The missing check is which one.

So each org gets a roster of CCNs in `orgs.json`, and every route that accepts
one checks it:

```js
app.get("/api/scorecard", (req, res) => {
  access.scope.assertCcn(req, req.query.ccn);   // 403 unless it is theirs
  res.json(vault.scorecard(req.query.ccn));
});
```

```json
{
  "pratt": { "name": "Pratt Regional Medical Center", "ccns": ["170027"] }
}
```

An org with no entry sees nothing rather than everything: a missing line in a
config file should be a support ticket, not a breach. Startup rejects a CCN
sold to two orgs, and a CCN written as a JSON number — `070027` parses as
`70027`, matches nothing, and the customer just sees an empty picker.

The denial says the same thing for a hospital that belongs to someone else and
one that does not exist. Telling those apart turns the endpoint into a list of
who is a customer.

### Reading across hospitals

Two routes have to, and refusing them outright removes the product.
`/api/state-ranking` scores every hospital in a state and returns them by name;
that is a competitive-intelligence file on 120 organizations, and no customer
bought it. But *"you rank 31st of 120"* is a fact about the subscriber, and that
is the part they did buy.

`scope.summarize()` draws the line. The caller's own rows come back whole; the
peers come back as a percentile rank and — only above the cohort floor — a
quartile band, on `benchmark.js`'s rules:

```js
res.json(scope.summarize(req, payload, {
  valueKey: "score",
  cohortLabel: state + " hospitals",
  higherIsBetter: true,
}));
```

No peer names, no peer values, no minimum, no maximum. A `minerva_admin` gets
the payload untouched, so the internal view is not degraded by a control meant
for customers.

## Per-caller state

`server.js` kept three values at module scope — one copy each, for the whole
process:

```js
let uploadedDoc  = null;   // spliced into the prompt of whoever chats next
let lastEnvelope = null;   // what /api/export writes into a .docx
let lastAgentId  = DEFAULT_AGENT;
```

Correct for a single-user demo, wrong the moment a second organization signs in.
One client's uploaded budget becomes context in another client's conversation;
`/api/export` builds a memo from another client's scorecard. Neither needs a
mistake by a user — they leak by working as written, and both callers passed
every check at the door.

`TenantState` keys that state by caller rather than by organization. Per-org
would close the cross-tenant hole, but two colleagues at the same hospital would
still find each other's uploads appearing mid-conversation. Entries expire, so a
chat server does not accumulate the full text of every document anyone has ever
sent.

```js
app.post("/api/upload", upload.single("file"), (req, res) => {
  tenants.for(req).uploadedDoc = { name, text };
});
```

`/health` reported the uploaded document's *filename*, unauthenticated. It
reports a caller count now.

## It refuses to start

Without `ACCESS_TEAM_DOMAIN` and `ACCESS_AUD` there is no way to verify a token,
and the only two behaviours available are "reject everything" or "trust
everything". `bootAccess` throws, with both variable names in the message.

A server that will not start is a five-minute problem with an obvious cause. A
server that starts and serves every hospital's data to whoever finds the port is
the other kind.

There is a development path, because refusing to run on a laptop is how a
control ends up commented out:

```bash
MINERVA_DEV_IDENTITY=cfo@prattregional.org node server.js
```

It prints a banner on first use, refuses to load when `NODE_ENV=production`, and
refuses an identity that `directory.json` would itself deny — otherwise the
first thing anyone meets in production is a 403 they never saw locally.

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

## The two config files

Copy both examples and keep both out of git. `directory.json` is a list of your
customers' staff; `orgs.json` is a list of what each customer bought.

```bash
cp access/directory.example.json access/directory.json
cp access/orgs.example.json access/orgs.json
```

One says who works for whom, the other says which hospitals they may read.
They only mean anything together, and every way of getting them wrong looks
fine in an editor — so check what they actually grant before restarting:

```bash
node access/preflight.js --app ~/minerva-4.0
```

```
email                      role            sees
-------------------------  --------------  ----
cfo@prattregional.org      org_admin       1 hospital: 170027
jeff@minervaai.health      minerva_admin   every hospital
cfo@hutchinson.org         org_admin       NOTHING

  PROBLEM  cfo@hutchinson.org: org "hutchinson" has no entry in orgs.json
```

None of those are syntax errors. A user whose org is missing from `orgs.json`
signs in successfully and sees an empty picker with nothing logged anywhere; an
org with hospitals and no staff is a customer who cannot log in; and a directory
with no `minerva_admin` locks you out of your own product. Each one shows up as
a person who cannot use the thing, on the morning of the demo.

The one thing it cannot check is Cloudflare's own list. An address here that
Access does not admit never arrives; an address Access admits that is missing
here gets `403 not_provisioned`. After a failed sign-in, the exact address
Access asserted is the last line of `access-audit.log`.

## The directory

`directory.json` is a list of your customers' staff. An email Access admits but the directory does
not list is denied (`403 not_provisioned`) rather than given a default org.

Malformed entries fail at startup, not at request time: an unknown role, a
missing org, or a non-staff account holding org `"*"` all throw when the guard
is built.

## Tests

```bash
node --test "access/test/*.test.js"
```

123 tests, no dependencies. The interesting ones are the denials — forged
signature, `alg: none`, wrong audience, wrong team, expired, unprovisioned
email, unreachable key endpoint, a CCN from another organization, a decision
verified by id rather than by hospital — since those are what protect the pilot.

Three negative properties are asserted rather than described:

- whatever the cohort looks like, a benchmark response carries no peer list and
  neither extreme;
- a state ranking serialized for a customer contains no other hospital's CCN and
  no other hospital's name;
- after the server patch, no route taking a `?ccn=` is left unchecked, and the
  patched file still parses.

The last one matters because a string-replacing patcher's characteristic failure
is a syntax error, and `pm2` will restart a broken server sixteen times before
giving up quietly.
