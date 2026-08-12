/**
 * Generate the provider-context content bank from the payer-context original.
 *
 * The build spec ships Form A with four scenario items written for a payer
 * (member appeals, plan provisions) and notes: "Build provider-side variants if
 * the deployment population requires it." This script produces that variant.
 *
 * Only the four scenario items change. IDs, dimensions, option keys, correct
 * answers, weights, and every scoring rule stay identical, so the scoring
 * engine and its tests are unaffected — the two forms are interchangeable.
 *
 * Constructs preserved exactly:
 *   D3.4  verify a plausible-looking citation against its source before use
 *   D3.5  recognize which task fails in a way that is hard to detect
 *   D4.4  acceptable handling of protected information under a BAA
 *   D4.5  accountability does not transfer to the tool or vendor
 *
 * Items stay administrative: no clinical decision support, no treatment or
 * diagnostic judgment is ever the subject of a question.
 *
 * Run: node make-provider-form.js
 * Out: minerva_readiness_content.provider.json
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SRC = path.join(__dirname, "minerva_readiness_content.json");
const OUT = path.join(__dirname, "minerva_readiness_content.provider.json");

const PROVIDER_SCENARIOS = {
  "D3.4": {
    text:
      "An AI tool summarizes a payer's medical-necessity policy and cites a specific coverage criterion as the basis for a prior-authorization request. The citation looks plausible and the summary reads cleanly. Before you rely on this summary, what do you do first?",
    options: [
      { key: "A", text: "Use it — the summary is internally consistent.", correct: false },
      { key: "B", text: "Open the payer's policy and confirm the cited criterion says what the summary claims.", correct: true },
      { key: "C", text: "Ask the AI tool whether it is confident in the citation.", correct: false },
      { key: "D", text: "Run the same prompt again and see whether you get the same answer.", correct: false },
    ],
  },
  "D3.5": {
    text: "Which of these tasks is AI most likely to get wrong in a way that is hard to detect?",
    options: [
      { key: "A", text: "Reformatting a charge description master extract into a different layout.", correct: false },
      { key: "B", text: "Summarizing a policy document you have already read.", correct: false },
      { key: "C", text: "Reasoning about a novel case that combines several payer policies and CMS rules with no clear precedent.", correct: true },
      { key: "D", text: "Drafting a routine appointment reminder letter.", correct: false },
    ],
  },
  "D4.4": {
    text:
      "You want help drafting an appeal for a denied claim that involves a patient's clinical history. Which is acceptable?",
    options: [
      { key: "A", text: "Paste the patient's full chart into a personal AI account, since you will delete the chat afterward.", correct: false },
      { key: "B", text: "Use an organization-approved tool covered by a Business Associate Agreement, following your policy on what may be entered.", correct: true },
      { key: "C", text: "Replace the patient's name with initials and paste the rest into any AI tool.", correct: false },
      { key: "D", text: "Ask a colleague to paste it in from their account instead.", correct: false },
    ],
  },
  "D4.5": {
    text:
      "An AI tool you use daily produces an output that leads to a decision later found to be wrong. Who is accountable?",
    options: [
      { key: "A", text: "The vendor who built the tool.", correct: false },
      { key: "B", text: "Nobody — the error was in the model.", correct: false },
      { key: "C", text: "You and your organization; the tool does not transfer accountability.", correct: true },
      { key: "D", text: "The department that approved the tool.", correct: false },
    ],
  },
};

const content = JSON.parse(fs.readFileSync(SRC, "utf8"));

content.form_version = "A-provider";
content.deployment_context = "provider";

let changed = 0;
content.items = content.items.map((item) => {
  const swap = PROVIDER_SCENARIOS[item.id];
  if (!swap) return item;
  if (item.type !== "scenario") throw new Error(item.id + " is not a scenario item");
  changed += 1;
  return Object.assign({}, item, { text: swap.text, options: swap.options });
});

if (changed !== Object.keys(PROVIDER_SCENARIOS).length) {
  throw new Error("expected to swap " + Object.keys(PROVIDER_SCENARIOS).length + " items, swapped " + changed);
}

// Record provenance alongside the spec's existing notes.
content.notes = (content.notes || []).concat([
  "Provider-context form: the four scenario items are provider-side variants of the payer originals. Constructs, IDs, option keys, correct answers, and all scoring rules are unchanged, so this form is interchangeable with the payer form.",
]);

fs.writeFileSync(OUT, JSON.stringify(content, null, 2) + "\n");
console.log("wrote " + path.basename(OUT) + " (" + changed + " scenario items swapped)");
