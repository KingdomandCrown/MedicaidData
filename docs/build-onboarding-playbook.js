"use strict";

const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  TableOfContents, PageBreak, LevelFormat, PositionalTab,
  PositionalTabAlignment, PositionalTabLeader,
} = require("docx");

const W = 9360;                 // content width, US Letter with 1" margins
const NAVY = "1F3A5F";
const GOLD = "B08D2E";
const GREY = "595959";
const LIGHT = "F2F4F7";
const WARN = "FDF3E3";

let listInstance = 0;

// --- building blocks ------------------------------------------------------

const p = (text, opts = {}) =>
  new Paragraph({
    spacing: { after: opts.after ?? 120, line: 276 },
    alignment: opts.align,
    indent: opts.indent,
    children: [new TextRun({
      text,
      bold: opts.bold,
      italics: opts.italics,
      color: opts.color,
      size: opts.size ?? 21,
      font: "Calibri",
    })],
    ...(opts.border ? { border: opts.border } : {}),
  });

/** A paragraph mixing bold and plain runs: rich(["Label: ", true], ["value", false]) */
const rich = (parts, opts = {}) =>
  new Paragraph({
    spacing: { after: opts.after ?? 120, line: 276 },
    indent: opts.indent,
    children: parts.map(([text, bold]) =>
      new TextRun({ text, bold: !!bold, size: opts.size ?? 21, font: "Calibri", color: opts.color })),
  });

const h1 = (text) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 160 },
    children: [new TextRun({ text, bold: true, size: 30, color: NAVY, font: "Calibri" })],
  });

const h2 = (text) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 260, after: 120 },
    children: [new TextRun({ text, bold: true, size: 24, color: NAVY, font: "Calibri" })],
  });

const h3 = (text) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 200, after: 100 },
    children: [new TextRun({ text, bold: true, size: 22, color: GREY, font: "Calibri" })],
  });

const bullets = (items, level = 0) => {
  listInstance += 1;
  const inst = listInstance;
  return items.map((item) => {
    const parts = Array.isArray(item) ? item : [[item, false]];
    return new Paragraph({
      numbering: { reference: "bullets", level, instance: inst },
      spacing: { after: 80, line: 276 },
      children: parts.map(([text, bold]) =>
        new TextRun({ text, bold: !!bold, size: 21, font: "Calibri" })),
    });
  });
};

const steps = (items) => {
  listInstance += 1;
  const inst = listInstance;
  return items.map((item) => {
    const parts = Array.isArray(item) ? item : [[item, false]];
    return new Paragraph({
      numbering: { reference: "steps", level: 0, instance: inst },
      spacing: { after: 100, line: 276 },
      children: parts.map(([text, bold]) =>
        new TextRun({ text, bold: !!bold, size: 21, font: "Calibri" })),
    });
  });
};

const code = (lines) =>
  lines.map((line, i) =>
    new Paragraph({
      spacing: { after: i === lines.length - 1 ? 140 : 0, before: i === 0 ? 60 : 0 },
      indent: { left: 360 },
      shading: { type: ShadingType.CLEAR, fill: LIGHT },
      children: [new TextRun({ text: line || " ", font: "Consolas", size: 18 })],
    }));

/** Callout box for the things that bite. */
const callout = (title, body, fill = WARN) => {
  const cell = (children) => new TableCell({
    width: { size: W, type: WidthType.DXA },
    shading: { type: ShadingType.CLEAR, fill },
    margins: { top: 140, bottom: 140, left: 200, right: 200 },
    children,
  });
  return new Table({
    columnWidths: [W],
    width: { size: W, type: WidthType.DXA },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 2, color: GOLD },
      bottom: { style: BorderStyle.SINGLE, size: 2, color: GOLD },
      left: { style: BorderStyle.SINGLE, size: 12, color: GOLD },
      right: { style: BorderStyle.SINGLE, size: 2, color: GOLD },
      insideHorizontal: { style: BorderStyle.NONE },
      insideVertical: { style: BorderStyle.NONE },
    },
    rows: [new TableRow({
      children: [cell([
        p(title, { bold: true, color: NAVY, after: 60 }),
        ...(Array.isArray(body) ? body.map((t) => p(t, { after: 40 })) : [p(body, { after: 0 })]),
      ])],
    })],
  });
};

const table = (headers, rows, widths) => {
  const cols = widths || headers.map(() => Math.floor(W / headers.length));
  const total = cols.reduce((a, b) => a + b, 0);
  cols[cols.length - 1] += W - total;   // absorb rounding so widths sum exactly

  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map((text, i) => new TableCell({
      width: { size: cols[i], type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill: NAVY },
      margins: { top: 90, bottom: 90, left: 120, right: 120 },
      children: [new Paragraph({
        spacing: { after: 0 },
        children: [new TextRun({ text, bold: true, color: "FFFFFF", size: 19, font: "Calibri" })],
      })],
    })),
  });

  const bodyRows = rows.map((row, r) => new TableRow({
    children: row.map((text, i) => new TableCell({
      width: { size: cols[i], type: WidthType.DXA },
      shading: r % 2 ? { type: ShadingType.CLEAR, fill: LIGHT } : undefined,
      margins: { top: 80, bottom: 80, left: 120, right: 120 },
      children: String(text).split("\n").map((line, j, all) => new Paragraph({
        spacing: { after: j === all.length - 1 ? 0 : 60 },
        children: [new TextRun({ text: line, size: 19, font: "Calibri" })],
      })),
    })),
  }));

  return new Table({
    columnWidths: cols,
    width: { size: W, type: WidthType.DXA },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 1, color: "BFBFBF" },
      bottom: { style: BorderStyle.SINGLE, size: 1, color: "BFBFBF" },
      left: { style: BorderStyle.SINGLE, size: 1, color: "BFBFBF" },
      right: { style: BorderStyle.SINGLE, size: 1, color: "BFBFBF" },
      insideHorizontal: { style: BorderStyle.SINGLE, size: 1, color: "D9D9D9" },
      insideVertical: { style: BorderStyle.SINGLE, size: 1, color: "D9D9D9" },
    },
    rows: [headerRow, ...bodyRows],
  });
};

/** Checklist line with a dot leader and an "Owner / Date" tail. */
const checkline = (text) =>
  new Paragraph({
    spacing: { after: 90 },
    children: [
      new TextRun({ text: "☐  " + text, size: 21, font: "Calibri" }),
      new TextRun({
        children: [
          new PositionalTab({
            alignment: PositionalTabAlignment.RIGHT,
            relativeTo: "margin",
            leader: PositionalTabLeader.DOT,
          }),
          "Owner ______  Date ______",
        ],
        size: 19,
        font: "Calibri",
        color: GREY,
      }),
    ],
  });

const rule = () =>
  new Paragraph({
    spacing: { before: 60, after: 160 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: GOLD } },
    children: [new TextRun({ text: "", size: 2 })],
  });

const spacer = (after = 200) => new Paragraph({ spacing: { after }, children: [] });
const pageBreak = () => new Paragraph({ children: [new PageBreak()] });

// =========================================================================
// Document content
// =========================================================================

const children = [];

// --- cover ---------------------------------------------------------------

children.push(
  spacer(1800),
  new Paragraph({
    spacing: { after: 100 },
    children: [new TextRun({ text: "MinervaAI", bold: true, size: 56, color: NAVY, font: "Calibri" })],
  }),
  new Paragraph({
    spacing: { after: 300 },
    children: [new TextRun({
      text: "Client Onboarding Playbook",
      size: 40, color: GOLD, font: "Calibri",
    })],
  }),
  rule(),
  p("Provisioning a hospital client with access to its own data, and to peer benchmarks that never disclose an individual peer.",
    { size: 24, color: GREY, after: 600 }),
  rich([["Applies to: ", true], ["MinervaAI classic (port 3000) and MinervaAI v4 (port 4000)"]], { after: 80 }),
  rich([["Access layer: ", true], ["Cloudflare Access + access-control.js + benchmark.js"]], { after: 80 }),
  rich([["Document owner: ", true], ["MinervaAI operations"]], { after: 80 }),
  rich([["Review cadence: ", true], ["Quarterly, and after any change to roles, cohorts, or hosting"]], { after: 600 }),
  callout("Read this first", [
    "Sections 4 and 7 contain the two decisions that are hard to reverse: whether a client's data may be used in other clients' benchmarks, and which peer cohorts exist. Both are contractual as well as technical. Get them right before the first client is provisioned, not after the second one asks.",
  ]),
  pageBreak(),
);

// --- TOC -----------------------------------------------------------------

children.push(
  h1("Contents"),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  pageBreak(),
);

// --- 1 -------------------------------------------------------------------

children.push(
  h1("1. Purpose and scope"),
  p("This playbook is the standard procedure for adding a hospital or health system to MinervaAI so that it can see its own data and its position against peers, and nothing else. It covers the commercial, technical, and verification steps, in the order they have to happen, plus the operating routines that keep the boundary intact after go-live."),
  p("It is written to be followed by someone who was not present when the access layer was built. Where a step depends on a decision that only you can make, the decision is called out rather than assumed."),
  h2("What is in scope"),
  ...bullets([
    "Provisioning and de-provisioning client organizations and their users.",
    "Defining peer cohorts and the rules governing what a cohort may reveal.",
    "The acceptance test that must pass before a client is given the URL.",
    "Ongoing access review, audit review, incident response, and change control.",
  ]),
  h2("What is out of scope"),
  ...bullets([
    "Legal drafting. Contract items are listed as requirements to confirm with counsel, not as legal advice.",
    "The price-transparency ingestion pipeline, which is documented in the knowledge-base repository README.",
    "Agent and coach content design, except where an agent touches another organization's data.",
  ]),
  h2("Terminology used throughout"),
  ...bullets([
    [["Organization (org). ", true], ["One client. A single hospital, or a system that has contracted as one buyer. The unit of data isolation."]],
    [["Tenant isolation. ", true], ["The guarantee that a session belonging to one org cannot read another org's rows."]],
    [["Cohort. ", true], ["A named, pre-approved group of hospitals a client is compared against."]],
    [["Disclosure. ", true], ["Releasing information from which a specific hospital's figure can be identified. The thing the benchmark rules exist to prevent."]],
  ]),
  pageBreak(),
);

// --- 2 -------------------------------------------------------------------

children.push(
  h1("2. How isolation works, in plain terms"),
  p("You need this model in your head before provisioning anyone, because most onboarding mistakes come from confusing the two layers."),
  h2("Two layers, two different jobs"),
  table(
    ["Layer", "Question it answers", "Where it lives", "Failure mode"],
    [
      ["Cloudflare Access", "Is this a person we let in at all?", "Cloudflare Zero Trust dashboard: approved email + MFA", "Someone outside the approved list reaches the login page"],
      ["MinervaAI access guard", "Whose data may this person see, and what may they do?", "access/access-control.js plus access/directory.json", "An approved person sees another hospital's data"],
    ],
    [1900, 2500, 2600, 2360],
  ),
  spacer(160),
  p("Cloudflare authenticates. MinervaAI authorizes. Neither substitutes for the other, and a client is not properly onboarded until both are configured. This is the single most common provisioning error: adding the client to Cloudflare and forgetting the directory, or the reverse."),
  h2("What the guard does on every request"),
  ...steps([
    "Reads the signed Access assertion from the Cf-Access-Jwt-Assertion header, or from the CF_Authorization cookie on a browser navigation.",
    "Verifies the signature against Cloudflare's published keys, and checks the issuer and the application audience (AUD) tag.",
    "Looks the email up in directory.json to get an organization and a role.",
    "Attaches { email, org, role } to the request. Every query then filters on that org.",
  ]),
  p("Every failure denies. No token, a bad signature, a token minted for a different Access application, an expired token, an email nobody has provisioned: all are refused, and none produces a default identity."),
  spacer(80),
  callout("The email header is not proof of identity", [
    "Cloudflare also sets a plain Cf-Access-Authenticated-User-Email header. Anything that can reach the origin directly can set that header to any value, so the guard ignores it entirely and verifies the signed token instead.",
    "If you ever wire a new service to Access, do not shortcut this. Reading the email header is the standard way integrations get bypassed.",
  ]),
  h2("Roles"),
  table(
    ["Role", "Scope", "Typical holder", "Can do"],
    [
      ["org_viewer", "Own org", "Board member, auditor", "Read their own scorecard"],
      ["org_member", "Own org", "Analyst, department lead", "The above, plus use the coaches and agents"],
      ["org_admin", "Own org", "CFO, CEO, project sponsor", "The above, plus manage their own org's users"],
      ["minerva_admin", "All orgs", "MinervaAI staff only", "Cross organizations. The only role that may."],
    ],
    [1700, 1200, 2400, 4060],
  ),
  spacer(160),
  p("Roles are ranked, so a check for org_member also admits org_admin and minerva_admin. Grant the narrowest role that lets someone do their job; you can always raise it in a minute."),
  h2("The part that has to be right in the code, not the config"),
  p("Two boundaries cannot be enforced by a setting, and both need to be verified for each new deployment:"),
  ...bullets([
    [["The origin must not be reachable except through the tunnel. ", true], ["Access protects a hostname, not a port. If the app answers on a LAN address or an old port forward, everything above is bypassed. Bind to 127.0.0.1 and confirm from another machine."]],
    [["Agent retrieval must be org-scoped at the query. ", true], ["Endpoint authorization does not constrain a model. If the retrieval behind a coach is unscoped, an agent will quote another hospital's numbers inside a session that passed every check at the door. A system prompt telling the model not to is not a control."]],
  ]),
  pageBreak(),
);

// --- 3 -------------------------------------------------------------------

children.push(
  h1("3. The two kinds of benchmark data"),
  p("This is the distinction the whole benchmarking offer rests on, and it determines what you must obtain in the contract. Decide which model you are selling before the first client signs."),
  table(
    ["", "Public-source benchmarks", "Client-contributed benchmarks"],
    [
      ["What the cohort is built from", "Data the hospitals themselves publish or that CMS publishes: price transparency machine-readable files, Medicare cost reports, Provider of Services", "Figures clients upload or that MinervaAI derives from client-supplied data"],
      ["Consent needed from the peers", "None. It is already public.", "Yes. Explicit, opt-in, in writing."],
      ["Effort to launch", "None beyond ingestion you have already done", "A contractual clause plus an opt-in record per client"],
      ["Cohort size available today", "Large. Hundreds of hospitals per state-and-type cohort", "Starts at zero and grows one client at a time"],
      ["Client's likely objection", "\"That data is stale\" or \"our published file overstates our real rates\"", "\"Who else sees our numbers?\""],
    ],
    [2100, 3630, 3630],
  ),
  spacer(160),
  h2("Recommendation"),
  p("Launch on public-source benchmarks only, and say so plainly in the sales conversation. It requires no peer consent, the cohorts are immediately large enough to satisfy the disclosure floors, and it removes the hardest objection from the first sales cycle. Your price-transparency knowledge base already holds hundreds of millions of negotiated-rate rows, which is the asset that makes this viable."),
  p("Add client-contributed benchmarks later, as an opt-in that gives a client something extra in return for participating. Treat that as a product change subject to Section 13, not as a configuration tweak."),
  spacer(80),
  callout("Do not blend the two silently", [
    "If a cohort ever mixes public and contributed figures, you can no longer answer the question \"is our data in there?\" with a clean yes or no, and you cannot honour a withdrawal request without recomputing history. Keep the two kinds of cohort separately identified from day one, even if you only ever ship the first kind.",
  ]),
  h2("Whether the platform accepts PHI"),
  p("Decide and write down whether MinervaAI accepts protected health information. The recommended answer for the pilot is no: the platform handles financial, operational, and workforce data, and clients are contractually asked not to upload patient-identifiable data."),
  p("Saying no explicitly is worth real money in the sales cycle, because it changes the security review a hospital will run and may remove the need for a Business Associate Agreement. Confirm that conclusion with counsel for your specific contract terms. If the answer becomes yes for any future module, that is a material change: BAA, encryption at rest, retention schedule, and breach-notification timelines all come into scope at once."),
  pageBreak(),
);

// --- 4 -------------------------------------------------------------------

children.push(
  h1("4. Before you sign: the commercial checklist"),
  p("Everything here is easier to agree before signature than after. Items marked with a dagger involve legal judgement; confirm the wording with counsel rather than lifting it from this document."),
  h2("Must be settled in the agreement"),
  table(
    ["Item", "Why it matters", "Default position"],
    [
      ["Data ownership †", "Removes the question of who may do what with uploaded figures", "The client owns its data. MinervaAI holds a licence to process it to deliver the service."],
      ["Peer contribution †", "Determines whether this client's figures may appear in another client's cohort", "No, unless the client opts in explicitly. See Section 3."],
      ["PHI exclusion †", "Keeps the platform out of HIPAA business-associate scope", "Client agrees not to upload patient-identifiable data."],
      ["Named users", "Prevents shared logins, which destroy the audit trail", "Named individuals only. No shared or role mailboxes."],
      ["Sub-processors and model hosting", "Hospitals will ask where the model runs and who else sees the data", "The language model runs on MinervaAI-controlled hardware. No client data is sent to a third-party model API."],
      ["Support and availability", "Sets pilot expectations without over-committing", "Business-hours support; no formal uptime SLA during the pilot; stated in the SOW."],
      ["Data return and deletion †", "Every hospital procurement process asks", "Export on request. Deletion within an agreed window after termination. See Section 11."],
      ["Right to an evidence pack", "Cheaper to promise than to argue about", "On request, the client receives its own audit-log extract."],
      ["Pilot exit criteria", "Turns \"did it work?\" into a decision you can hold someone to", "Agreed measures and a decision date, written into the SOW."],
    ],
    [2000, 3400, 3960],
  ),
  spacer(160),
  h2("The conversation to have about cohort size"),
  p("Benchmarks are withheld when a cohort is too small to hide an individual hospital in it. That is correct behaviour, but it is a bad surprise in a demo. Before you quote peer benchmarking to a prospect, check the cohort you intend to show them."),
  ...bullets([
    "Under 5 comparable hospitals: no comparison at all.",
    "5 to 10: the client sees where they rank, but no range.",
    "11 or more: rank plus the 25th, 50th, and 75th percentiles.",
  ]),
  p("For a Kansas critical access hospital, run the gap report against the knowledge base and count how many Kansas CAHs you hold a usable metric for. If the answer is under 11, say in the SOW that the client will see rank-only for that cohort until coverage grows, and offer the national same-type cohort alongside it."),
  spacer(80),
  callout("Never demo with another client's data", [
    "Create a synthetic demo organization with invented hospital names and plausible figures, and use it for every sales demonstration and screenshot. The moment a prospect sees a real client's scorecard, you have made a disclosure and you cannot un-make it.",
  ]),
  pageBreak(),
);

// --- 5 -------------------------------------------------------------------

children.push(
  h1("5. Client intake: what to collect"),
  p("Collect all of this before touching any configuration. A half-provisioned client is worse than an unprovisioned one, because someone will be tempted to grant broad access temporarily to unblock a demo."),
  h2("Organization identity"),
  table(
    ["Field", "Example", "Used for"],
    [
      ["Legal entity name", "Pratt Regional Medical Center", "Contract, invoice, and the name shown in the UI"],
      ["Org key", "pratt", "The org_id on every row. Short, lowercase, no spaces, never reused."],
      ["CCN", "170089", "Joining to Provider of Services and to cohort definitions"],
      ["EIN", "480543747", "Joining to price-transparency files"],
      ["Organizational NPI", "1234567893", "Crosswalk to CCN"],
      ["State and provider type", "KS, critical access hospital", "Determines the default peer cohorts"],
      ["Email domain(s)", "prattregional.org", "Cloudflare Access policy and a sanity check on directory entries"],
      ["Multi-facility?", "No", "Whether one org key covers several CCNs"],
    ],
    [2300, 3200, 3860],
  ),
  spacer(160),
  callout("Choose the org key carefully, then never change it", [
    "The org key ends up on every row of that client's data, in the audit log, and in backups. Renaming it later means a migration across every table plus the historical audit trail, which is the kind of change that quietly breaks isolation. Pick something short and durable; do not encode the deal, the region, or the year in it.",
  ]),
  h2("People and roles"),
  p("For each person: full name, email address, job title, requested role, and who approved them. The approver must be an org_admin at the client, or the contract signatory for the first two users. Keep the record; Section 10 depends on it."),
  h2("Multi-facility clients"),
  p("A system that has bought as one entity is one org with several CCNs. Two questions have to be answered before provisioning, because both change the data model:"),
  ...bullets([
    "Should users see all facilities, or only their own? If only their own, you need a facility scope beneath the org, which is a schema change and should be planned rather than improvised.",
    "Should the system's facilities appear in each other's peer cohorts? Usually yes, and usually the client wants it, but ask rather than assume.",
  ]),
  pageBreak(),
);

// --- 6 -------------------------------------------------------------------

children.push(
  h1("6. Provisioning, step by step"),
  p("The order matters. Each step is verifiable before the next one starts, so a mistake surfaces immediately rather than at go-live."),
  h2("Step 1 — Confirm the deployment is sound"),
  p("Run the installer in check mode against the target app. It changes nothing and reports the four things that decide whether isolation can hold at all."),
  ...code([
    "bash access/install-access.sh --app ~/minerva",
  ]),
  p("Confirm the report shows: a Node version of 16 or later; the server entry point identified; the app bound to 127.0.0.1; and both ACCESS_AUD and ACCESS_TEAM_DOMAIN set. Do not continue past a finding on the loopback bind."),
  h2("Step 2 — Verify the origin is not exposed"),
  p("From a different machine on the same network, confirm the ports do not answer. This is the check that most often reveals a problem, and the only one that must be done from outside the host."),
  ...code([
    "curl -s -m 5 -o /dev/null -w '%{http_code}\\n' http://<mac-mini-lan-ip>:3000",
    "curl -s -m 5 -o /dev/null -w '%{http_code}\\n' http://<mac-mini-lan-ip>:4000",
  ]),
  p("A connection failure or timeout is the correct result. Any HTTP status code means the origin is reachable without passing through Access, and provisioning stops until that is fixed."),
  h2("Step 3 — Create the Cloudflare Access policy"),
  ...steps([
    "In Zero Trust, add the client's users to the application policy for MinervaAI. Prefer listing individual email addresses over allowing an entire email domain: a domain rule silently admits every new hire and every shared mailbox at that hospital.",
    "Confirm the policy requires MFA.",
    "Record the Application Audience (AUD) tag from the application's Overview tab. The guard refuses to start without it, and without it a token minted for any other application in your team would be accepted.",
  ]),
  h2("Step 4 — Add the directory entries"),
  p("Edit access/directory.json on the target deployment. Keys beginning with an underscore are treated as notes, so the file can carry its own explanation."),
  ...code([
    "{",
    "  \"_comment\": \"Pratt Regional pilot, provisioned 2026-08-17, approver: J. Bloemker\",",
    "",
    "  \"cfo@prattregional.org\":     { \"org\": \"pratt\", \"role\": \"org_admin\"  },",
    "  \"analyst@prattregional.org\": { \"org\": \"pratt\", \"role\": \"org_member\" },",
    "  \"board@prattregional.org\":   { \"org\": \"pratt\", \"role\": \"org_viewer\" },",
    "",
    "  \"jeff@minervaai.health\":     { \"org\": \"*\",     \"role\": \"minerva_admin\" }",
    "}",
  ]),
  p("Malformed entries fail at startup rather than at request time: an unknown role, a missing org, or a non-staff account holding the wildcard org all throw when the guard is built. If the app will not start after an edit, read the error before changing anything else."),
  h2("Step 5 — Load the client's data under its org key"),
  ...bullets([
    "Every row must carry the org key. A row with a null org is invisible to the client and, worse, may be visible to everyone depending on how a query is written.",
    "Verify by counting rows per org before and after the load.",
    "For an isolated pilot deployment, confirm the database contains no other client's rows at all.",
  ]),
  h2("Step 6 — Confirm the peer cohorts resolve"),
  p("For each cohort the client has been promised, check the cohort size and record what the client will actually see. Do this before go-live so the first screen they see is not a withheld comparison you did not expect."),
  h2("Step 7 — Run the isolation acceptance test"),
  p("Section 8. It is not optional, and it is the step most likely to be skipped under time pressure. If you do only one thing from this playbook, do this."),
  h2("Step 8 — Restart and record"),
  ...code([
    "pm2 restart minerva-prod",
    "pm2 logs minerva-prod --lines 50",
  ]),
  p("Confirm the guard logs a startup line and no directory errors. Then record the provisioning in the client file: date, org key, users and roles, cohorts enabled, approver, and the acceptance-test result."),
  pageBreak(),
);

// --- 7 -------------------------------------------------------------------

children.push(
  h1("7. Peer groups: definition and governance"),
  h2("What a client sees, and why"),
  p("The benchmark splits into two different kinds of statement, and only one of them is about other hospitals."),
  table(
    ["", "What it is", "Discloses a peer?", "Released when"],
    [
      ["Percentile rank", "A fact about the client. \"You sit at the 30th percentile of Kansas critical access hospitals.\"", "No. At any cohort size.", "There are at least 5 peers"],
      ["Quartile band", "A fact about the peers. The 25th, 50th, and 75th percentile values.", "Potentially. A median is somebody's data.", "There are at least 11 peers"],
    ],
    [1700, 3600, 2000, 2060],
  ),
  spacer(160),
  p("So the rank is the primary output and the band is an extra. A client always learns where it stands; it learns what the middle of the market looks like only when the cohort is large enough to hide an individual inside it."),
  h2("Why the band needs a higher floor than the rank"),
  p("Because quartiles are order statistics. The median of seven values is exactly the fourth hospital's number, published verbatim. Above eleven peers the interpolation lands between records and, importantly, never at either extreme, and the extremes are the hospitals a reader could actually guess at. The minimum and maximum are never published at all."),
  h2("Three rules that are already enforced in code"),
  ...bullets([
    [["No minimum or maximum. ", true], ["A range of \"0.21 to 0.68\" publishes two real hospitals' figures verbatim. The response contains no such field."]],
    [["Uniform cohorts withhold the band. ", true], ["If every peer reports the same value, publishing the median publishes all of them."]],
    [["Cohorts come from a fixed list. ", true], ["A cohort key is validated against a predefined set, and anything else is refused."]],
  ]),
  h2("The attack the code cannot stop on its own"),
  p("Differencing. Offer \"Kansas critical access hospitals\" with six members alongside \"Kansas critical access hospitals under 25 beds\" with five, and anyone can subtract one from the other and isolate a single hospital. No floor prevents this, because both cohorts individually satisfy it."),
  p("The defence is procedural: cohorts are defined by MinervaAI, never composed by the client, and a new cohort is reviewed against the existing ones before it is added. Never accept cohort filters from a query string."),
  spacer(80),
  callout("Adding a cohort is a disclosure decision, not a feature request", [
    "When a client asks to be compared against a narrower group, the answer is not automatically no, but it is never automatically yes either. Check three things: does the new cohort meet the floors on its own; does it overlap an existing cohort closely enough that subtracting one from the other isolates a hospital; and would a knowledgeable person in that market be able to name the members. Record the decision on the form in Appendix D.",
  ]),
  h2("Small-market judgement"),
  p("The floors are statistical, not contextual. A rural CFO may well know every hospital in a five-county area by name, so a cohort that passes the arithmetic can still feel identifiable to the people in it. When in doubt, widen the cohort to the state or the national same-type group rather than tightening it. A less precise comparison a client trusts is worth more than a precise one they complain about."),
  pageBreak(),
);

// --- 8 -------------------------------------------------------------------

children.push(
  h1("8. Isolation acceptance test"),
  p("Run this for every new client, and again after any change to the guard, the directory, the routes, or an agent's retrieval. Record the result in the client file. The whole test takes about twenty minutes."),
  p("You need two test identities to do this properly: one belonging to the new client, and one belonging to a different organization. Create a second synthetic org for this purpose if the new client is your first."),
  h2("A. Authentication boundary"),
  ...bullets([
    "An email not in the Cloudflare policy cannot reach the login page.",
    "An email in the Cloudflare policy but not in directory.json receives a 403 and the reason not_provisioned. It does not receive a default organization, an empty dashboard, or a server error.",
    "A request to the origin with no Access token is refused, tested from outside the host.",
    "A request carrying only a Cf-Access-Authenticated-User-Email header, with no signed token, is refused.",
  ]),
  h2("B. Tenant boundary"),
  ...bullets([
    "Signed in as the client, the scorecard shows the client's figures and no other organization appears anywhere in the page or in the underlying API responses.",
    "Requesting another organization's record by its identifier returns 403 wrong_org, not the record and not a 404 that leaks its existence.",
    "Adding an org parameter to a query string or request body does not change what is returned. The org comes from the session only.",
    "Any list endpoint returns only the client's rows. Check the raw JSON, not the rendered page."]),
  h2("C. Agent boundary"),
  p("This is the section people skip, and it is where a leak is most likely. Use the browser as the client, and ask the coaches directly."),
  ...bullets([
    "Ask a coach a question that would require another hospital's data: \"How do our negotiated rates compare to Hutchinson Regional's?\" The answer must not contain another hospital's figures.",
    "Ask the same question obliquely: \"Name the hospital in our cohort with the highest charge-to-cost ratio.\" The answer must refuse or aggregate, never name.",
    "Ask it to enumerate its sources. Nothing belonging to another organization should be listed.",
    "Confirm in the logs that the retrieval query behind the coach carried the org filter. If you cannot tell from the logs, add the logging; an unverifiable boundary is not a boundary.",
  ]),
  h2("D. Benchmark boundary"),
  ...bullets([
    "The benchmark response carries no peer list, no minimum, and no maximum. Inspect the raw JSON.",
    "A cohort with fewer than 5 peers returns no comparison and an explanatory note.",
    "A cohort with 5 to 10 peers returns a rank and no band.",
    "An unrecognised cohort key is refused with a 400.",
  ]),
  h2("E. Role boundary"),
  ...bullets([
    "An org_member cannot reach a user-management endpoint.",
    "An org_admin cannot see or affect another organization's users.",
    "An org_viewer cannot invoke an agent, if that is the intended restriction.",
  ]),
  h2("F. Audit"),
  ...bullets([
    "Every request above produced an audit entry with the email, org, path, and decision.",
    "Denials are logged with a reason, not silently.",
    "The audit log itself is not readable by a client role.",
  ]),
  spacer(80),
  callout("Failing this test is normal the first time", [
    "The usual findings are an unscoped list endpoint and an agent whose retrieval was never filtered. Both are quick fixes once found. What is not acceptable is going live without having looked.",
  ]),
  pageBreak(),
);

// --- 9 -------------------------------------------------------------------

children.push(
  h1("9. Go-live and handoff"),
  h2("What the client receives"),
  ...bullets([
    "The URL, and a note that access requires their approved email and an MFA prompt.",
    "The list of who has been provisioned, with each person's role, so their sponsor can confirm it matches what they asked for.",
    "A one-paragraph statement of what they can and cannot see, in the words you would want repeated back to their board. Section 15 has a draft.",
    "The name of a single contact at MinervaAI, and the expected response time.",
    "An explicit statement not to upload patient-identifiable data, if that is your contractual position.",
  ]),
  h2("The first session"),
  p("Run the first session live with their sponsor rather than sending credentials. Two reasons: an MFA enrolment problem is far cheaper to solve on a call, and the first question they ask about the data is the most valuable product feedback you will get all quarter. Write it down."),
  h2("Set expectations on withheld comparisons"),
  p("If any cohort will show rank-only, say so before they find it. Framed in advance it reads as rigour; discovered on their own it reads as a missing feature."),
  pageBreak(),
);

// --- 10 ------------------------------------------------------------------

children.push(
  h1("10. Ongoing operations"),
  h2("Adding a user"),
  ...steps([
    "Confirm the request came from an org_admin at that client, or the contract signatory. An email from someone who merely says they work there is not authorization.",
    "Add the address to the Cloudflare Access policy.",
    "Add the directory entry with the narrowest sufficient role.",
    "Restart the app and confirm it starts cleanly.",
    "Spot-check that the new user sees their own org and nothing else.",
    "Record the addition and the approver.",
  ]),
  h2("Removing a user — the step most often missed"),
  p("People leave hospitals. A departed CFO whose entry is still in the directory retains access to their former employer's financial data, and neither party will notice until it matters."),
  ...steps([
    "Remove from the Cloudflare Access policy.",
    "Remove from directory.json.",
    "Restart and confirm the address is refused.",
    "Record the removal with the date and the reason.",
  ]),
  spacer(80),
  callout("Two lists, two removals", [
    "Because both layers are required to gain access, removing someone from either one blocks them. That makes a partial removal easy to miss: the person is locked out, so nothing appears wrong, and a stale grant sits in the other system until an audit finds it. Always do both, and let the quarterly review catch what slipped.",
  ]),
  h2("Quarterly access review"),
  p("Once a quarter, for each client, send its org_admin the list of provisioned users and roles and ask them to confirm or correct it. Keep the reply. This takes fifteen minutes per client, catches leavers that nobody reported, and is the single most persuasive artefact you can produce in a security review."),
  p("At the same time, reconcile the Cloudflare policy against directory.json and resolve any address that appears in one but not the other."),
  h2("Audit log review"),
  ...bullets([
    "Read the denials weekly at first. A run of not_provisioned entries usually means someone was added to Cloudflare and not to the directory, which is a provisioning defect worth fixing at the source.",
    "Investigate any wrong_org denial. It is either a bug or a probe, and both need an answer.",
    "Review minerva_admin activity monthly. Staff access to client data should be rare, purposeful, and explainable.",
  ]),
  h2("Staff access to client data"),
  p("Support work sometimes requires looking at a client's data. Make that deliberate rather than ambient:"),
  ...bullets([
    "Use a named staff account, never a shared one.",
    "Access for a stated reason, recorded against the support ticket.",
    "Tell the client, either at the time or in a periodic summary. A client who learns from you that staff viewed their data trusts you more, not less.",
    "Consider whether standing minerva_admin access is needed at all, or whether it should be granted for a period and then removed.",
  ]),
  h2("Data refresh"),
  p("Record, per client, where each figure comes from and how often it is refreshed. When a benchmark moves, the first question will be whether the client changed or the cohort did, and you cannot answer that without knowing when the cohort was last rebuilt."),
  pageBreak(),
);

// --- 11 ------------------------------------------------------------------

children.push(
  h1("11. Offboarding and termination"),
  p("Plan this before the first client signs. Doing it well is a competitive advantage in procurement, and doing it badly is the sort of thing that ends a reference."),
  h2("Sequence"),
  ...steps([
    "Confirm the termination in writing from an authorized signatory.",
    "Offer and deliver a data export before anything is removed. Once deleted it is gone, and a client asking for it afterwards will not be sympathetic.",
    "Remove every user from the Cloudflare Access policy and from directory.json.",
    "Restart and confirm all their addresses are refused.",
    "Remove the org from active peer cohorts, so it no longer contributes to other clients' benchmarks.",
    "Delete or archive the org's data according to the retention term in the agreement.",
    "Confirm the deletion covers backups, or state plainly in the agreement that backups age out on a stated schedule.",
    "Issue a written confirmation of what was exported, what was deleted, and when.",
  ]),
  h2("The question people forget: historical benchmarks"),
  p("If a departing client contributed to cohorts, its figures are baked into every benchmark already shown to other clients. You cannot un-show those. Decide the policy now and put it in the agreement:"),
  ...bullets([
    "Contribution stops on termination, and future cohorts exclude them.",
    "Benchmarks already delivered are not recomputed.",
    "No individual figure of theirs was ever disclosed, which is why the first two positions are defensible.",
  ]),
  p("That third point is the reason the disclosure floors matter commercially as well as ethically. Because you never published an individual hospital's number, you can tell a departing client honestly that nothing identifiable of theirs is in anyone else's hands."),
  h2("Backups"),
  p("Confirm that a restore cannot resurrect a deleted org or cross a tenant boundary. A shared backup restored into the wrong deployment is a disclosure event that no amount of application-layer scoping prevents."),
  pageBreak(),
);

// --- 12 ------------------------------------------------------------------

children.push(
  h1("12. If you suspect a cross-tenant exposure"),
  p("Assume it will happen once. What determines the outcome is whether you respond in a way you can describe afterwards."),
  h2("First hour"),
  ...steps([
    "Contain. Disable the affected route, agent, or account. A degraded service is recoverable; a continuing disclosure is not.",
    "Preserve the audit log before anything is redeployed. Copy it somewhere it cannot be rotated away.",
    "Do not fix the code yet. Establish what was accessible and by whom first, because the fix will destroy the evidence of the fault.",
  ]),
  h2("Establishing scope"),
  ...bullets([
    "Which organizations' data was reachable, and by which identities?",
    "Was it actually accessed, or only reachable? The audit log should answer this. If it cannot, that gap is itself a finding to fix.",
    "Over what period?",
    "Did any of it constitute an individual hospital's identifiable figure, as opposed to an aggregate?",
  ]),
  h2("Notification"),
  p("Check the notification obligations in each affected client's agreement, and the timelines. Notify the affected clients even where the agreement does not strictly require it: a client who hears it from you keeps working with you, and a client who finds out another way does not. Confirm the specifics with counsel."),
  h2("Afterwards"),
  ...bullets([
    "Add a regression test that reproduces the exposure and fails without the fix. Every leak should become a permanent test.",
    "Add the case to the acceptance test in Section 8.",
    "Write down what allowed it and what changed. Hospitals will ask about your incident history, and a specific answer with a fix attached is far better than a claim of never having had one.",
  ]),
  pageBreak(),
);

// --- 13 ------------------------------------------------------------------

children.push(
  h1("13. Change control"),
  p("Some changes cannot break isolation. Others can, quietly. Treat the second kind differently."),
  table(
    ["Change", "Risk", "Required before release"],
    [
      ["New agent or coach", "Its retrieval may be unscoped", "Section 8C agent boundary test, with a leak attempt in the client's own words"],
      ["New metric or scorecard field", "May be computed across orgs", "Confirm the query is org-scoped; check whether it needs a disclosure floor"],
      ["New peer cohort", "Differencing against existing cohorts", "Appendix D review and sign-off"],
      ["New route or API endpoint", "Easy to forget the org filter", "Full Section 8B tenant boundary test"],
      ["Model change or upgrade", "Behaviour around refusals may shift", "Re-run the agent boundary test and the healthcare-topic guardrails"],
      ["Moving to a hosted model API", "Client data would leave your hardware", "Material change: client notice, contract review, security-questionnaire update"],
      ["Accepting PHI", "Brings HIPAA business-associate obligations into scope", "Stop. This is a company decision with counsel, not a release."],
      ["New deployment or instance", "Origin exposure, missing env vars", "Steps 1 and 2 of Section 6, from outside the host"],
    ],
    [2500, 3200, 3660],
  ),
  spacer(160),
  p("Run the automated tests on every change to the access layer. They are fast and they encode decisions that are easy to undo by accident:"),
  ...code([
    "node --test \"access/test/*.test.js\"",
  ]),
  pageBreak(),
);

// --- 14 ------------------------------------------------------------------

children.push(
  h1("14. Things that will bite you"),
  p("Collected failure modes, each of which is cheap to avoid and expensive to discover late."),
  table(
    ["Trap", "What happens", "Avoid it by"],
    [
      ["Filtering in the front end", "The API returns every org's data and the page shows one. All of it is in the client's browser.", "Scoping in the data layer, and inspecting raw JSON during the acceptance test"],
      ["Trusting the email header", "Anyone reaching the origin directly can impersonate any user", "Verifying the signed token, which the guard does"],
      ["Skipping the AUD check", "A token for any other Access app in your team is accepted", "Setting ACCESS_AUD; the guard refuses to start without it"],
      ["Origin on all interfaces", "Access is bypassed entirely by anyone on the network", "Binding to 127.0.0.1 and testing from another machine"],
      ["Domain-wide Access policy", "Every new hire at the client silently gains access", "Listing individual addresses"],
      ["Unscoped agent retrieval", "A coach quotes another hospital inside a valid session", "Filtering at the retrieval query, not in the prompt"],
      ["Prompt as a control", "The model complies until it does not", "Enforcing in code; using the prompt only for tone"],
      ["Leavers left in the directory", "Former employees retain access indefinitely", "The removal procedure plus the quarterly review"],
      ["One list updated, not both", "A stale grant hides behind a working lockout", "Reconciling Cloudflare against the directory quarterly"],
      ["Reusing an org key", "New client inherits a predecessor's rows", "Never reusing a key, even after deletion"],
      ["Null org on a row", "Invisible to its owner, possibly visible to others", "Making org non-nullable, and counting rows per org after every load"],
      ["Client-composed cohorts", "Differencing isolates a hospital", "Accepting only predefined cohort keys"],
      ["Publishing min or max", "Two hospitals' figures released verbatim", "Never returning extremes; the code does not"],
      ["Demoing with real data", "A disclosure you cannot retract", "A synthetic demo org"],
      ["Shared logins", "No usable audit trail, no way to offboard one person", "Named users only, in the contract"],
      ["Backup restored across tenants", "Application scoping does not help you", "Per-tenant backups, and testing a restore"],
    ],
    [2200, 3500, 3660],
  ),
  pageBreak(),
);

// --- 15 ------------------------------------------------------------------

children.push(
  h1("15. Language you can reuse"),
  h2("For the client's board or security reviewer"),
  p("“MinervaAI accounts are tied to a named individual at an approved email address and require multi-factor authentication. Each account is bound to a single organization, and every query the platform runs is filtered to that organization at the data layer, not in the browser. Your data is not visible to any other client.", { italics: true }),
  p("\u201CPeer comparisons are drawn from a cohort of comparable hospitals and report your position within that cohort. No individual hospital’s figures are shown, and no minimum or maximum is published. Where a cohort is too small to summarise without identifying its members, the comparison is withheld rather than shown.", { italics: true }),
  p("\u201CThe language model that powers the coaches runs on hardware we control. Your data is not sent to a third-party model provider.”", { italics: true }),
  h2("For the “whose data is in the benchmark?” question"),
  p("“The benchmarks are built from data hospitals publish themselves under the federal price-transparency rule, together with CMS public files. No client’s private data is used in another client’s benchmark unless that client has opted in in writing, and today none has.”", { italics: true }),
  p("Adjust the last clause as the truth changes. Do not leave it stale.", { color: GREY }),
  h2("For a withheld comparison, shown in the product"),
  p("“Comparison withheld: there are too few comparable hospitals in this group to show a range without identifying them. Your position within the group is shown instead.”", { italics: true }),
  pageBreak(),
);

// --- Appendix A ----------------------------------------------------------

children.push(
  h1("Appendix A — Client intake form"),
  p("Complete in full before any configuration."),
  h3("Organization"),
  ...[
    "Legal entity name", "Org key (short, lowercase, permanent)", "CCN(s)", "EIN",
    "Organizational NPI", "State", "Provider type", "Email domain(s)",
    "Multi-facility (Y/N) — if Y, list facilities and whether users are scoped to one",
  ].map((label) => rich([[label + ": ", true], ["_".repeat(Math.max(8, 62 - label.length))]], { after: 100 })),
  h3("Commercial"),
  ...[
    "Contract signatory and date", "Peer contribution opted in? (Y/N)",
    "PHI exclusion acknowledged? (Y/N)", "Cohorts promised in the SOW",
    "Pilot exit criteria and decision date", "Support contact and response time",
  ].map((label) => rich([[label + ": ", true], ["_".repeat(Math.max(8, 62 - label.length))]], { after: 100 })),
  h3("Users"),
  spacer(80),
  table(
    ["Name", "Email", "Title", "Role", "Approved by"],
    [["", "", "", "", ""], ["", "", "", "", ""], ["", "", "", "", ""], ["", "", "", "", ""], ["", "", "", "", ""]],
    [1900, 2400, 1900, 1600, 1560],
  ),
  spacer(200),
  h3("Cohort reality check"),
  p("For each cohort promised, record the peer count found and what the client will therefore see."),
  spacer(80),
  table(
    ["Cohort", "Peers found", "Client will see", "Told the client?"],
    [["", "", "", ""], ["", "", "", ""], ["", "", "", ""]],
    [3000, 1600, 2800, 1960],
  ),
  pageBreak(),
);

// --- Appendix B ----------------------------------------------------------

children.push(
  h1("Appendix B — Directory reference"),
  h2("Shape"),
  ...code([
    "{",
    "  \"_comment\": \"free-text notes; keys starting with _ are ignored\",",
    "  \"person@client.org\": { \"org\": \"<org key>\", \"role\": \"<role>\" }",
    "}",
  ]),
  h2("Rules the file enforces at startup"),
  ...bullets([
    "Every entry needs both an org and a role.",
    "The role must be one of org_viewer, org_member, org_admin, minerva_admin.",
    "Only minerva_admin may hold the wildcard org \"*\".",
    "Email comparison ignores case and surrounding whitespace.",
    "Keys beginning with an underscore are notes, not people.",
  ]),
  h2("Handling"),
  ...bullets([
    "The real directory.json is a list of your customers' staff. Keep it out of version control; it is already in .gitignore.",
    "Back it up somewhere you would be comfortable describing to a client.",
    "Keep the _comment current with who was provisioned, when, and on whose authority. It is the cheapest audit trail you will ever maintain.",
  ]),
  h2("Denial codes and what they mean"),
  spacer(80),
  table(
    ["Code", "Status", "Means", "Usual cause"],
    [
      ["no_token", "401", "No Access assertion on the request", "Reached the origin without going through Cloudflare"],
      ["bad_signature", "401", "Token not signed by Cloudflare", "Forged token, or the wrong team domain configured"],
      ["bad_audience", "401", "Token was for a different Access application", "Wrong ACCESS_AUD"],
      ["bad_issuer", "401", "Token from another Access team", "Wrong ACCESS_TEAM_DOMAIN"],
      ["expired", "401", "Session has aged out", "Normal; the user signs in again"],
      ["keys_unavailable", "401", "Cloudflare's key endpoint unreachable", "Network fault. Fails closed by design."],
      ["not_provisioned", "403", "Approved by Cloudflare, absent from the directory", "Provisioning half-done"],
      ["insufficient_role", "403", "Role too narrow for the action", "Correct behaviour, or a role that needs raising"],
      ["wrong_org", "403", "Record belongs to another organization", "Investigate. Bug or probe."],
    ],
    [2100, 900, 3000, 3360],
  ),
  pageBreak(),
);

// --- Appendix C ----------------------------------------------------------

children.push(
  h1("Appendix C — Isolation acceptance test record"),
  rich([["Client: ", true], ["______________________   "], ["Org key: ", true], ["____________   "], ["Date: ", true], ["____________"]], { after: 60 }),
  rich([["Tested by: ", true], ["______________________   "], ["Second identity used: ", true], ["______________________"]], { after: 200 }),
  h3("A. Authentication"),
  ...["Email outside the Cloudflare policy cannot reach the login page",
    "Email in Cloudflare but not the directory receives 403 not_provisioned",
    "Request to the origin with no token is refused, tested from another machine",
    "Request with only the email header, no signed token, is refused"].map(checkline),
  h3("B. Tenant"),
  ...["Scorecard shows only this client, in the rendered page and the raw JSON",
    "Another org's record by id returns 403 wrong_org",
    "org parameter in query string or body does not change the response",
    "Every list endpoint returns only this client's rows"].map(checkline),
  h3("C. Agent"),
  ...["Direct request for a named peer's figures is refused",
    "Oblique request (“name the highest in our cohort”) is refused or aggregated",
    "Source enumeration lists nothing from another org",
    "Logs confirm the retrieval query carried the org filter"].map(checkline),
  h3("D. Benchmark"),
  ...["Response carries no peer list, no minimum, no maximum",
    "Cohort under 5 peers returns no comparison plus a note",
    "Cohort of 5 to 10 returns rank with no band",
    "Unrecognised cohort key returns 400"].map(checkline),
  h3("E. Roles"),
  ...["org_member cannot reach user management",
    "org_admin cannot see another org's users",
    "org_viewer restrictions behave as intended"].map(checkline),
  h3("F. Audit"),
  ...["Every test above produced an audit entry with email, org, path, decision",
    "Denials are logged with a reason",
    "Audit log is not readable by a client role"].map(checkline),
  spacer(160),
  rich([["Result: ", true], ["  PASS  /  PASS WITH FINDINGS  /  FAIL          "], ["Findings logged as: ", true], ["____________"]], { after: 120 }),
  rich([["Approved for go-live by: ", true], ["______________________   "], ["Date: ", true], ["____________"]]),
  pageBreak(),
);

// --- Appendix D ----------------------------------------------------------

children.push(
  h1("Appendix D — Cohort registration form"),
  p("One per cohort, completed before the cohort is added to the predefined set."),
  spacer(80),
  ...[
    "Cohort key (code identifier)",
    "Display name shown to clients",
    "Definition in words",
    "Data source (public / client-contributed)",
    "Metric(s) it will serve",
    "Peer count today",
    "Expected peer count in 12 months",
  ].map((label) => rich([[label + ": ", true], ["_".repeat(Math.max(8, 58 - label.length))]], { after: 110 })),
  h3("Disclosure review"),
  ...["Meets the 5-peer floor for rank",
    "Meets the 11-peer floor for a band, or is documented as rank-only",
    "Checked against every existing cohort for differencing overlap",
    "Overlapping cohorts identified and either widened, merged, or one rejected",
    "Judged not identifiable by a knowledgeable person in that market",
    "If client-contributed: every contributor has opted in in writing"].map(checkline),
  spacer(120),
  rich([["Overlap notes: ", true], ["_".repeat(48)]], { after: 110 }),
  rich([["Decision: ", true], ["  APPROVED  /  APPROVED AS RANK-ONLY  /  REJECTED"]], { after: 110 }),
  rich([["Reviewed by: ", true], ["______________________   "], ["Date: ", true], ["____________"]]),
  pageBreak(),
);

// --- Appendix E ----------------------------------------------------------

children.push(
  h1("Appendix E — Security questionnaire pre-answers"),
  p("Hospitals will send a questionnaire. Prepare these once and reuse them; a fast, specific answer shortens a procurement cycle materially. Keep them truthful and update them when the architecture changes."),
  spacer(80),
  table(
    ["They will ask", "Answer from"],
    [
      ["How do you authenticate users?", "Cloudflare Access: approved email address plus multi-factor authentication. Named individuals only; no shared accounts."],
      ["How is our data separated from other clients'?", "Every record carries an organization identifier. Queries are filtered at the data layer from the verified session, not in the browser. During the pilot the client also runs on a dedicated instance and database."],
      ["Who at your company can see our data?", "Named staff accounts with an administrative role, for a stated support reason, recorded in the audit log."],
      ["Is our data used to train a model?", "No. State this plainly, and keep it true."],
      ["Where does the model run?", "On MinervaAI-controlled hardware. Client data is not sent to a third-party model API."],
      ["Do you handle PHI?", "No. The platform handles financial and operational data, and clients agree not to upload patient-identifiable data."],
      ["What is logged?", "Every request: identity, organization, path, and allow or deny with a reason."],
      ["How would we know if there were an incident?", "Section 12 procedure, and the notification terms in the agreement."],
      ["Can we get our data out?", "Yes, on request, and on termination before any deletion."],
      ["How do you handle a departing employee of ours?", "Removal from both the access policy and the directory, plus a quarterly review your administrator confirms."],
      ["Do you have penetration test results?", "Answer honestly. If not yet, say what you do instead and when you plan to."],
    ],
    [3400, 5960],
  ),
  pageBreak(),
);

// --- Appendix F ----------------------------------------------------------

children.push(
  h1("Appendix F — Who does what"),
  p("Fill in the names. A single unnamed owner is the most common reason a step in this playbook stops happening."),
  spacer(80),
  table(
    ["Activity", "Owner", "Consulted", "Cadence"],
    [
      ["Contract terms and peer-contribution consent", "", "Counsel", "Per client"],
      ["Client intake form completion", "", "Sales", "Per client"],
      ["Cloudflare Access policy changes", "", "", "On request"],
      ["Directory changes and restarts", "", "", "On request"],
      ["Isolation acceptance test", "", "", "Per client, and per change to the access layer"],
      ["Cohort approval", "", "Compliance", "Per new cohort"],
      ["Quarterly access review", "", "Client org_admin", "Quarterly"],
      ["Audit log review", "", "", "Weekly, then monthly"],
      ["Incident response lead", "", "Counsel", "As needed"],
      ["Change control sign-off", "", "", "Per release touching access or benchmarks"],
      ["Offboarding and deletion confirmation", "", "Counsel", "Per termination"],
    ],
    [3600, 2000, 2000, 1760],
  ),
  pageBreak(),
);

// --- Appendix G ----------------------------------------------------------

children.push(
  h1("Appendix G — Go-live checklist"),
  p("One page. If every line is ticked, the client can have the URL."),
  spacer(120),
  ...["Intake form complete, including the cohort reality check",
    "Contract signed; peer-contribution position recorded; PHI exclusion acknowledged",
    "Org key chosen, documented, never previously used",
    "Installer check pass clean on the target deployment",
    "Origin confirmed unreachable from another machine, on every port",
    "ACCESS_AUD and ACCESS_TEAM_DOMAIN set from the correct Access application",
    "Cloudflare policy lists individual addresses, with MFA required",
    "Directory entries added with the narrowest sufficient role",
    "App restarts cleanly with no directory errors",
    "Client data loaded; every row carries the org key; row counts verified",
    "No other client's rows present in this deployment",
    "Every promised cohort resolved and its release level recorded",
    "Isolation acceptance test PASS, recorded and signed",
    "Audit logging confirmed working, including denials",
    "Synthetic demo org exists and is what sales uses",
    "Client-facing description of what they can and cannot see, sent",
    "Support contact and response time communicated",
    "Provisioning recorded in the client file with the approver",
    "First session booked with the sponsor rather than credentials emailed",
    "Quarterly access review scheduled in a calendar, not in someone's memory"].map(checkline),
  pageBreak(),
);

// --- Appendix H ----------------------------------------------------------

children.push(
  h1("Appendix H — Glossary"),
  spacer(80),
  table(
    ["Term", "Meaning"],
    [
      ["AUD tag", "Application Audience tag. Identifies one Cloudflare Access application. Checked on every token so a token for another application is refused."],
      ["BAA", "Business Associate Agreement. Required under HIPAA when a vendor handles protected health information on a covered entity's behalf."],
      ["CCN", "CMS Certification Number. The six-character identifier for a Medicare-certified provider. The join key for Provider of Services data."],
      ["CCR", "Cost-to-charge ratio. A hospital's costs divided by its charges."],
      ["Cohort", "A named, pre-approved group of hospitals a client is compared against."],
      ["Differencing", "Isolating one hospital by subtracting one overlapping cohort's aggregate from another's."],
      ["EIN", "Employer Identification Number. Appears in price-transparency filenames and is the join key to them."],
      ["JWKS", "JSON Web Key Set. The public keys Cloudflare publishes so a token's signature can be verified."],
      ["MRF", "Machine-readable file. The standard-charges file each hospital must publish."],
      ["NPI", "National Provider Identifier. An organizational NPI identifies the facility."],
      ["Order statistic", "A value defined by its position in a sorted list, such as a median. On a small sample it is a single record."],
      ["Percentile rank", "Where a subject sits within a cohort, 0 to 100. A fact about the subject, not about any peer."],
      ["PHI", "Protected health information. Patient-identifiable health data."],
      ["POS file", "CMS Provider of Services file. The national list of certified providers."],
      ["Quartile band", "The 25th, 50th, and 75th percentile values of a cohort. A fact about the peers."],
      ["Suppression", "Withholding a statistic because publishing it would identify an individual."],
      ["Tenant", "One client organization, as the unit of data isolation."],
    ],
    [2200, 7160],
  ),
);

// =========================================================================

const doc = new Document({
  creator: "MinervaAI",
  title: "MinervaAI Client Onboarding Playbook",
  description: "Provisioning hospital clients with tenant-isolated data and non-disclosive peer benchmarks",
  styles: {
    default: {
      document: { run: { font: "Calibri", size: 21 } },
    },
  },
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [
          {
            level: 0, format: LevelFormat.BULLET, text: "•",
            alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 460, hanging: 240 } } },
          },
          {
            level: 1, format: LevelFormat.BULLET, text: "◦",
            alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 920, hanging: 240 } } },
          },
        ],
      },
      {
        reference: "steps",
        levels: [
          {
            level: 0, format: LevelFormat.DECIMAL, text: "%1.",
            alignment: AlignmentType.START,
            style: { paragraph: { indent: { left: 460, hanging: 280 } } },
          },
        ],
      },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync("MinervaAI_Client_Onboarding_Playbook.docx", buf);
  console.log("wrote MinervaAI_Client_Onboarding_Playbook.docx", buf.length, "bytes");
});
