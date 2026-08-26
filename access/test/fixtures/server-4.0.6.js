// Fixture: the anchor-bearing regions of minerva-4.0/server.js at v4.0.6.
//
// Not the whole file -- the branded-docx rendering in the middle is ~200 lines
// that no anchor touches, and copying it here would only make this harder to
// compare against the real thing. Every line the patcher matches on is present
// verbatim, which is what the test needs to be worth running.
const express = require("express");
const path = require("path");
const fs = require("fs");
const multer = require("multer");
const pdfParse = require("pdf-parse");
const mammoth = require("mammoth");
const { AGENTS, SCHEMA } = require("./agents");
const vault = require("./vault");

const app = express();
app.use(express.json({ limit: "5mb" }));
app.use(express.static(path.join(__dirname, "public"), {
  setHeaders: (res, filePath) => {
    if (filePath.endsWith(".html")) res.set("Cache-Control", "no-store, must-revalidate");
  }
}));
app.use("/brand", express.static(path.join(__dirname, "brand")));

const OLLAMA = process.env.OLLAMA_URL || "http://localhost:11434";
const DEFAULT_MODEL = process.env.MINERVA_MODEL || "qwen3:32b";
let AVAILABLE_MODELS = [];
const TEMP = Number(process.env.MINERVA_TEMP || 0.3);

async function refreshModels() { AVAILABLE_MODELS = []; }
function effectiveDefault() {
  if (AVAILABLE_MODELS.some(m => m.id === DEFAULT_MODEL)) return DEFAULT_MODEL;
  return AVAILABLE_MODELS.length ? AVAILABLE_MODELS[0].id : DEFAULT_MODEL;
}
function resolveModel(requested) {
  if (requested && AVAILABLE_MODELS.some(m => m.id === requested)) return requested;
  return effectiveDefault();
}

const agentById = Object.fromEntries(AGENTS.map(a => [a.id, a]));
const DEFAULT_AGENT = "ask-minerva";

// In-memory demo-scope state
let uploadedDoc = null;          // { name, text }
let lastEnvelope = null;         // last validated scorecard envelope (for export)
let lastAgentId = DEFAULT_AGENT;

// ---------- Upload ----------
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 25 * 1024 * 1024 } });

app.post("/api/upload", upload.single("file"), async (req, res) => {
  try {
    if (!req.file) return res.status(400).json({ error: "No file received" });
    const name = req.file.originalname;
    const ext = name.split(".").pop().toLowerCase();
    let text = "";

    if (ext === "pdf") {
      const parsed = await pdfParse(req.file.buffer);
      text = parsed.text || "";
    } else if (ext === "docx") {
      const result = await mammoth.extractRawText({ buffer: req.file.buffer });
      text = result.value || "";
    } else if (ext === "txt" || ext === "md" || ext === "csv") {
      text = req.file.buffer.toString("utf8");
    } else {
      return res.status(400).json({ error: "Supported formats: PDF, DOCX, TXT, MD, CSV" });
    }

    text = text.replace(/\s+\n/g, "\n").trim();
    uploadedDoc = { name, text };
    res.json({ ok: true, name, chars: text.length });
  } catch (e) {
    res.status(500).json({ error: "Could not read that file: " + String(e.message || e) });
  }
});

app.post("/api/clear-doc", (req, res) => { uploadedDoc = null; res.json({ ok: true }); });

// ---------- Agents ----------
app.get("/api/agents", (req, res) => {
  res.json({ agents: AGENTS.filter(a => !a.hidden), default: DEFAULT_AGENT });
});

// ---------- Hospitals (vault directory for the picker) ----------
app.get("/api/hospitals", (req, res) => {
  res.json({ hospitals: vault.list(), version: vault.version() });
});

// ---------- Single hospital record (for the instant Detect opportunity map) ----------
app.get("/api/hospital", (req, res) => {
  const h = vault.get(req.query.ccn);
  if (!h) return res.status(404).json({ error: "unknown ccn" });
  res.json({ hospital: h, meridian: vault.meridian(h.ccn), pctls: vault.pctls(h.ccn), maternal: vault.maternal(h.ccn) });
});

// ---------- Peer & best-in-class benchmark (computed from the vault) ----------
app.get("/api/benchmark", (req, res) => {
  const b = vault.benchmark(req.query.ccn);
  if (!b) return res.status(404).json({ error: "unknown ccn" });
  res.json(b);
});

// ---------- Hospital Health Scorecard ----------
app.get("/api/scorecard", (req, res) => {
  const h = vault.get(req.query.ccn);
  if (!h) return res.status(404).json({ error: "unknown ccn" });
  res.json({
    financials: vault.financials(req.query.ccn),
    financialScore: vault.financialScore(req.query.ccn),
    vintage: vault.finVintage(),
    hospital: { ccn: h.ccn, name: h.name, state: h.state, type: h.type, beds: h.beds }
  });
});

// ---------- Like-peer roster ----------
app.get("/api/peers", (req, res) => {
  const h = vault.get(req.query.ccn);
  if (!h) return res.status(404).json({ error: "unknown ccn" });
  const metric = req.query.metric || null;
  const roster = vault.peerRoster(req.query.ccn, metric);
  if (!roster) return res.status(404).json({ error: "no like-peer set for this hospital" });
  res.json(roster);
});

// ---------- State health ranking ----------
app.get("/api/state-ranking", (req, res) => {
  const state = String(req.query.state || "").toUpperCase().trim();
  if (!state) return res.status(400).json({ error: "state required" });
  const rows = [];
  const years = new Set();
  vault.list().filter(x => x.state === state).forEach(x => {
    const f = vault.financials(x.ccn);
    if (!f || !f.m) return;
    const score = vault.financialScore(x.ccn);
    if (score == null) return;
    if (f.yr) years.add(String(f.yr));
    const g = k => (f.m[k] ? f.m[k][0] : null);
    rows.push({
      ccn: x.ccn, name: x.name, system: x.system, type: vault.get(x.ccn) && vault.get(x.ccn).type,
      score, yr: f.yr, sw: !!f.sw,
      opm: g("opm"), dcg: g("dcg"), age: g("age"), cr: g("cr")
    });
  });
  rows.sort((a, b) => b.score - a.score);
  rows.forEach((r, i) => { r.rank = i + 1; });
  const yrs = Array.from(years).sort();
  res.json({ state, vintage: vault.finVintage(), count: rows.length,
    yearSpan: yrs.length ? { min: yrs[0], max: yrs[yrs.length - 1] } : null, hospitals: rows });
});

// ---------- Decisions ----------
const DECISIONS_FILE = path.join(__dirname, "decisions.json");
function loadDecisions() { try { return JSON.parse(fs.readFileSync(DECISIONS_FILE, "utf8")); } catch { return []; } }
function saveDecisions(arr) { try { fs.writeFileSync(DECISIONS_FILE, JSON.stringify(arr, null, 2)); } catch (e) { console.warn("decisions save failed: " + e.message); } }

app.get("/api/decisions", (req, res) => {
  const all = loadDecisions();
  const ccn = req.query.ccn;
  res.json({ decisions: ccn ? all.filter(d => String(d.ccn) === String(ccn)) : all });
});
app.post("/api/decisions", (req, res) => {
  const b = req.body || {};
  if (!b.ccn || !b.decision) return res.status(400).json({ error: "ccn and decision are required" });
  const all = loadDecisions();
  const rec = {
    id: "d" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36),
    ts: new Date().toISOString(),
    ccn: String(b.ccn), hospitalName: b.hospitalName || "", officerId: b.officerId || "", officerName: b.officerName || "",
    decision: b.decision, play: b.play || "", owner: b.owner || "", verifyMetric: b.verifyMetric || "",
    target: b.target || "", verifyBy: b.verifyBy || "", vaultVersion: vault.version(),
    status: "open", outcome: "", moved: null, verifiedTs: ""
  };
  all.push(rec); saveDecisions(all);
  res.json({ ok: true, decision: rec });
});
app.post("/api/decisions/verify", (req, res) => {
  const b = req.body || {};
  const all = loadDecisions();
  const rec = all.find(d => d.id === b.id);
  if (!rec) return res.status(404).json({ error: "decision not found" });
  rec.status = "verified"; rec.outcome = b.outcome || ""; rec.moved = (b.moved === true || b.moved === "true");
  rec.verifiedTs = new Date().toISOString();
  saveDecisions(all);
  res.json({ ok: true, decision: rec });
});

// ---------- Models ----------
app.get("/api/models", async (req, res) => {
  if (!AVAILABLE_MODELS.length) await refreshModels();
  res.json({ models: AVAILABLE_MODELS, default: resolveModel(DEFAULT_MODEL) });
});

// ---------- Chat ----------
function buildPrompt(systemPrompt, messages, hospital) {
  let p = systemPrompt + "\n\n";
  const grounding = hospital ? vault.groundingText(hospital) : "";
  if (grounding) p += grounding + "\n\n";
  if (uploadedDoc) {
    const docText = uploadedDoc.text.slice(0, 9000);
    p += "The leader has uploaded a document named \"" + uploadedDoc.name + "\". " +
         "Use it as primary context when relevant.\n--- DOCUMENT START ---\n" + docText + "\n--- DOCUMENT END ---\n\n";
  }
  for (const m of messages) {
    p += (m.role === "user" ? "User: " : "Minerva: ") + m.content + "\n";
  }
  p += "Minerva:";
  return p;
}

function stripThink(s) {
  return String(s || "").replace(/<think>[\s\S]*?<\/think>/g, "").trim();
}

async function ollamaOnce(prompt, model) { return { text: "", ms: 0 }; }
function bakeJsonCheck(text) { return { valid: false }; }
function bakeFidelity(a, b) { return { unsupported: 0, outCount: 0, unsupportedExamples: [] }; }

app.post("/api/bakeoff-one", async (req, res) => {
  try {
    const model = req.body.model, ccn = req.body.ccn;
    if (!model || !ccn) return res.status(400).json({ error: "model and ccn are required" });
    if (!AVAILABLE_MODELS.length) await refreshModels();
    const h = vault.get(ccn); if (!h) return res.status(404).json({ error: "unknown ccn" });
    const grounding = vault.groundingText(h);
    const agent = agentById["officer-margin"] || agentById[DEFAULT_AGENT];
    const task = "Produce the financial resilience scorecard.";
    const prompt = buildPrompt(agent.prompt, [{ role: "user", content: task }], h);
    const { text, ms } = await ollamaOnce(prompt, model);
    const jc = bakeJsonCheck(text), fd = bakeFidelity(text, grounding);
    res.json({ model, ccn, name: h.name, ms, jsonValid: jc.valid });
  } catch (e) { res.status(500).json({ error: String(e.message || e) }); }
});
const BAKEOFF_DEFAULT_CCNS = ["171371", "261327", "261324"];
app.post("/api/bakeoff", async (req, res) => {
  res.setHeader("Content-Type", "application/x-ndjson");
  const emit = (o) => { try { res.write(JSON.stringify(o) + "\n"); } catch {} };
  emit({ type: "done" });
  res.end();
});

function validEnvelope(obj) { return !!obj; }
async function ollamaStream(prompt, useSchema, onChunk, onText, model) { return ""; }

app.post("/api/chat", async (req, res) => {
  res.setHeader("Content-Type", "application/x-ndjson");
  const emit = (obj) => { try { res.write(JSON.stringify(obj) + "\n"); } catch {} };
  try {
    const messages = Array.isArray(req.body.messages) ? req.body.messages.slice(-16) : [];
    let agent = agentById[req.body.agentId] || agentById[DEFAULT_AGENT];
    if (agent && (agent.disabled || agent.hidden)) agent = agentById[DEFAULT_AGENT];
    lastAgentId = agent.id;
    const hospital = vault.get(req.body.ccn);
    const useModel = resolveModel(req.body.model);
    const prompt = buildPrompt(agent.prompt, messages, hospital);

    let lastTick = 0;
    const onChunk = (chars) => {
      const now = Date.now();
      if (now - lastTick > 700) { lastTick = now; emit({ type: "progress", chars }); }
    };
    const onText = (delta) => emit({ type: "text", delta });

    emit({ type: "status", stage: "drafting", agent: agent.name });
    let envelope = null, schemaBroken = false;
    try {
      const raw1 = await ollamaStream(prompt, true, onChunk, onText, useModel);
      try { envelope = JSON.parse(raw1); } catch { envelope = null; }
    } catch (err) { schemaBroken = /500/.test(String(err)); if (!schemaBroken) throw err; }

    if (validEnvelope(envelope)) {
      if (envelope.response_type === "scorecard") lastEnvelope = envelope;
      emit({ type: "final", reply: envelope.memo_narrative, envelope,
        agent: { id: agent.id, name: agent.name }, model: useModel,
        doc: uploadedDoc ? uploadedDoc.name : null });
      return res.end();
    }

    emit({ type: "status", stage: "fallback" });
    const plain = await ollamaStream(prompt, false, onChunk, onText, useModel);
    emit({ type: "final", reply: plain || "(no response)", envelope: null, fallback: true,
      agent: { id: agent.id, name: agent.name }, model: useModel,
      doc: uploadedDoc ? uploadedDoc.name : null });
    res.end();
  } catch (e) {
    emit({ type: "final", reply: "error", envelope: null, error: String(e) });
    res.end();
  }
});

// ---------- Branded docx export ----------
function memoParagraphs(memoText) { return []; }
function scorecardTable(env) { return null; }
async function ollamaGenerate(prompt, useSchema, model) { return ""; }

app.post("/api/export", async (req, res) => {
  try {
    const messages = Array.isArray(req.body.messages) ? req.body.messages : [];
    if (!messages.length) return res.status(400).json({ error: "Nothing to export yet - have a conversation first." });
    const agent = agentById[req.body.agentId] || agentById[lastAgentId] || agentById[DEFAULT_AGENT];

    let convo = "";
    for (const m of messages) convo += (m.role === "user" ? "Leader: " : "Minerva: ") + m.content + "\n";
    const memoPrompt = "You are Minerva, drafting in the voice of the " + agent.name + ". " +
      (uploadedDoc ? "The conversation references an uploaded document named \"" + uploadedDoc.name + "\". " : "") +
      "--- CONVERSATION ---\n" + convo + "--- END ---\n\nMemo:";

    let memoText = stripThink(await ollamaGenerate(memoPrompt, false, resolveModel(req.body.model)));
    if (!memoText) memoText = "SUMMARY\nMemo generation returned no content. Please retry.";

    const bodyChildren = [];
    if (lastEnvelope && lastEnvelope.scorecard && Array.isArray(lastEnvelope.scorecard.rows) && lastEnvelope.scorecard.rows.length) {
      bodyChildren.push(lastEnvelope.scorecard.title.toUpperCase());
      bodyChildren.push(scorecardTable(lastEnvelope));
    }
    bodyChildren.push(...memoParagraphs(memoText));

    res.send(Buffer.from(JSON.stringify(bodyChildren)));
  } catch (e) {
    res.status(500).json({ error: "Export failed: " + String(e.message || e) });
  }
});

app.get("/health", (req, res) => res.json({
  status: "ok", version: "4.0.6", model: effectiveDefault(), models: AVAILABLE_MODELS.length, agents: AGENTS.length,
  doc: uploadedDoc ? uploadedDoc.name : null
}));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log("MinervaAI v4.0.6 running on http://localhost:" + PORT + " - " + AGENTS.length + " agents - default model " + DEFAULT_MODEL));
