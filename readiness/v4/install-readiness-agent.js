#!/usr/bin/env node
/* Register the AI Readiness Plan Composer in the MinervaAI v4 agent registry.
 *
 * Tailored to the v4 registry shape:
 *   const SCHEMA = {...}; const CONTRACT = `...`; const OFFICER_METHOD = `...`;
 *   const AGENTS = [ ... ];
 *   module.exports = { AGENTS, SCHEMA, CONTRACT };
 *
 * The agent returns its plan inside the EXISTING response envelope rather than a
 * new one, so it renders today with no UI work:
 *   scorecard rows  -> the six readiness dimensions vs their thresholds
 *   levers          -> the 90-day actions (intervention / evidence_source /
 *                      expected_impact / effort) which is the action library's shape
 *   gap_analysis    -> the placement narrative
 *   data_gaps       -> dimensions that could not be assessed
 * A dedicated readiness_plan object + renderer can come later, the way the
 * augmentation object works for the AI Job Augmentation Strategist.
 *
 * SAFE BY DEFAULT - dry run unless you pass --apply:
 *   node install-readiness-agent.js [appPath]           # show the plan, write nothing
 *   node install-readiness-agent.js [appPath] --apply   # back up, splice, validate, swap
 */
"use strict";
const fs = require("fs");
const path = require("path");

const APP = process.argv[2] && !process.argv[2].startsWith("--")
  ? process.argv[2] : "/Users/minervaai/minerva-4.0";
const APPLY = process.argv.includes("--apply");
const AGENTS_FILE = path.join(APP, "agents.js");

const NEW_ID = "ai-readiness-plan";
const NEW_NAME = "AI Readiness Plan Composer";
const NEW_GROUP = "Universal";
const NEW_TAGLINE = "Turns a readiness score into a 90-day plan - two dimensions, three actions a phase, grounded in your own policies and training.";
const NEW_STARTERS = [
  "Here are my readiness scores - build my 90-day plan.",
  "I landed in Blocked agency. What belongs to me and what belongs to my manager?",
  "Which two dimensions should I work on, and what do I actually do in the first four weeks?"
];

function die(m) { console.error("\n[abort] " + m + "\n"); process.exit(1); }

const PROMPT = [
"You are Minerva, the AI Readiness Plan Composer. You turn an individual's AI",
"readiness assessment into a 90-day development plan they will actually follow.",
"",
"WHAT YOU RECEIVE",
"Dimension scores (D1 Usage depth, D2 Delegation maturity, D3 Verification craft,",
"D4 Risk and data judgment, D5 Change orientation, D6 Organizational enablement),",
"the two axis indices (ICI across D1-D5, OEI = D6), a zone placement, gate flags,",
"the targeted dimensions, and a filtered action library. Scoring is done before",
"you are called, by a deterministic scorer. Never recompute, adjust, or dispute a",
"score - your job starts after the number.",
"",
"THE ZONES, AND WHAT EACH MEANS",
"- frontier: capable person, supported organization. Depth, not catch-up.",
"- emergent: supported, but needs structured practice on real work.",
"- blocked_agency: capability is AHEAD of what the organization's setup allows.",
"- early: foundations and basic access come first.",
"",
"HARD RULES ON PLAN SHAPE",
"1. Never exceed TWO targeted dimensions. Six targets is a curriculum, and",
"   curricula do not get finished. The targets arrive with the scores; use them.",
"2. Never exceed THREE actions per phase. The phases are Ground (weeks 1-4),",
"   Apply (weeks 5-8), and Consolidate (weeks 9-12).",
"3. In the blocked_agency zone, assign the MAJORITY of actions to the manager or",
"   to leadership, not to the employee. When the constraint is organizational,",
"   loading remediation onto the employee is both unfair and ineffective. Say",
"   plainly that the constraint sits with the organization.",
"4. Governance actions are MANDATORY and come first whenever the governance gate",
"   is set (risk and data judgment below the floor). Someone who delegates",
"   confidently without risk judgment is exposed, not mature.",
"5. Every action must name a specific, recurring work situation - not a general",
"   intention. \"When I draft any document over a page\" beats \"use AI more.\"",
"",
"GROUNDING - THE CREDIBILITY RULE",
"Ground every reference to a tool, policy, or training in the retrieved context.",
"If the retrieved block does not contain it, SAY SO rather than inventing a",
"resource. A hallucinated internal course name or policy title destroys trust on",
"first contact and is the worst failure you can produce here. Where an action",
"needs a real internal document and none was retrieved, keep the action and state",
"that the specific resource still needs to be identified.",
"",
"IF-THEN INTENTIONS",
"Close with two implementation intentions in the exact form",
"\"When [specific recurring situation], I will [specific AI-involving action].\"",
"Both clauses must be concrete; a vague intention is worse than none. This is the",
"highest-leverage element of the plan (Gollwitzer and Sheeran).",
"",
"HOW TO USE THE RESPONSE ENVELOPE",
"- response_type \"scorecard\" when composing or revising a plan.",
"- scorecard rows: one per dimension. metric = the dimension name; tier =",
"  \"structure\" for D6 (the organization's setup), \"process\" for D1/D2/D5,",
"  \"outcome\" for D3/D4 (the judgment that shows up in the work); actual = the",
"  score; benchmark = the placement threshold in play; benchmark_source = the",
"  readiness model and its published reference distribution; gap = points to the",
"  threshold; trend = \"unknown\" on a first assessment; status = red below 50,",
"  yellow 50-69, green 70+, gray when the dimension is insufficient.",
"- levers: the 90-day actions, in phase order. intervention = what the person or",
"  manager does, with its phase and owner named; evidence_source = the action's",
"  evidence basis; expected_impact = which dimension it moves and how; effort =",
"  low/medium/high from the action's effort in minutes.",
"- gap_analysis: the placement narrative in two to four sentences - where they",
"  stand, what drives it, and what the first four weeks change.",
"- data_gaps: any dimension marked insufficient, plus any internal resource the",
"  plan needs and the retrieved context did not supply.",
"- discussion_question: one question that starts the first action this week.",
"",
"VOICE AND BOUNDARY",
"Plain, direct, second person. Never congratulatory, never scolding. Name things",
"the person controls. This is a DEVELOPMENTAL instrument: it measures readiness,",
"not performance, and it may never be used for hiring, promotion, compensation,",
"or termination - say so if anyone asks you to use it that way. Self-assessed AI",
"skill is systematically miscalibrated; the scenario items correct for part of",
"that and not all of it. Placement thresholds are provisional until calibrated on",
"pilot data - never present a placement as a precise measurement."
].join("\n");

if (PROMPT.indexOf("`") >= 0 || PROMPT.indexOf("${") >= 0) die("internal: prompt contains a backtick or ${");

const NEWOBJ =
"  {\n" +
"    id: " + JSON.stringify(NEW_ID) + ",\n" +
"    recommended_model: \"qwen3:32b\",\n" +
"    rag: true,\n" +
"    group: " + JSON.stringify(NEW_GROUP) + ",\n" +
"    starters: [\n" +
NEW_STARTERS.map(s => "      " + JSON.stringify(s)).join(",\n") + "\n" +
"    ],\n" +
"    name: " + JSON.stringify(NEW_NAME) + ",\n" +
"    tagline: " + JSON.stringify(NEW_TAGLINE) + ",\n" +
"    prompt: `" + PROMPT + "` + CONTRACT\n" +
"  }";

if (!fs.existsSync(AGENTS_FILE)) {
  die("no agents.js at " + AGENTS_FILE + "\n         pass your v4 app path: node install-readiness-agent.js /path/to/minerva-4.0");
}
const src = fs.readFileSync(AGENTS_FILE, "utf8");
if (src.indexOf(NEW_ID) >= 0) { console.log("Already installed (" + NEW_ID + ") - nothing to do."); process.exit(0); }

const meIdx = src.indexOf("module.exports");
if (meIdx < 0) die("no module.exports found. Paste me the file.");
const closeIdx = src.lastIndexOf("];", meIdx);
if (closeIdx < 0) die("could not find the AGENTS array close. Paste me the file.");

let mod;
try { mod = require(AGENTS_FILE); } catch (e) { die("agents.js did not load: " + e.message); }
const arr = mod && mod.AGENTS;
if (!Array.isArray(arr)) die("module.exports.AGENTS is not an array.");
if (!mod.CONTRACT) die("this registry has no CONTRACT export - is this the v4 app? Check the path.");
const groups = {};
arr.forEach(a => { groups[a.group] = (groups[a.group] || 0) + 1; });
if (!groups[NEW_GROUP]) {
  console.log("note: no existing \"" + NEW_GROUP + "\" group; the card will start that section.");
}

console.log("app:          " + APP);
console.log("agents.js:    " + AGENTS_FILE + "  (" + arr.length + " agents)");
console.log("groups:       " + Object.keys(groups).map(g => g + "=" + groups[g]).join("  "));
console.log("insert point: end of AGENTS array, before line " +
  src.slice(0, closeIdx).split("\n").length + "'s  '];'");
console.log("new card:     \"" + NEW_NAME + "\"  group=" + NEW_GROUP + "  rag=true  model=qwen3:32b");

if (!APPLY) {
  console.log("\n---- object to be inserted (head) ----");
  console.log(NEWOBJ.split("\n").slice(0, 13).join("\n") + "\n    ... (prompt continues) ... ` + CONTRACT\n  }");
  console.log("\nDRY RUN - nothing was written. To install:");
  console.log("  node " + path.basename(process.argv[1]) + " " + APP + " --apply");
  process.exit(0);
}

const d = new Date(), p = n => String(n).padStart(2, "0");
const ts = "" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" +
  p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
const bak = AGENTS_FILE + ".bak." + ts;
fs.copyFileSync(AGENTS_FILE, bak);

const head = src.slice(0, closeIdx).replace(/\s+$/, "");
const tail = src.slice(closeIdx);
const out = head + (head.endsWith(",") ? "" : ",") + "\n" + NEWOBJ + "\n" + tail;

const staged = AGENTS_FILE + ".new.js";
fs.writeFileSync(staged, out);
let ok = false, err = "";
try {
  const chk = require(staged);
  const added = chk.AGENTS.find(a => a.id === NEW_ID);
  ok = Array.isArray(chk.AGENTS) && chk.AGENTS.length === arr.length + 1 && !!added &&
       !!chk.SCHEMA && !!chk.CONTRACT &&
       typeof added.prompt === "string" &&
       added.prompt.indexOf("VOICE") > 0;   // CONTRACT really got appended
} catch (e) { err = e.message; }
if (!ok) {
  try { fs.unlinkSync(staged); } catch (e) {}
  die("validation failed" + (err ? " (" + err + ")" : "") + " - original untouched. Backup: " + bak);
}
fs.renameSync(staged, AGENTS_FILE);
try { fs.writeFileSync(path.join(APP, ".readiness-agent-rollback"), bak + "\n"); } catch (e) {}

console.log("\nInstalled. Backup: " + bak);
console.log("Restart v4:  pm2 restart minerva-40");
console.log("The card appears in the " + NEW_GROUP + " section of the Agent Library.");
console.log("\nRollback:  ./rollback-readiness-agent.sh " + APP + "   (then pm2 restart)");
