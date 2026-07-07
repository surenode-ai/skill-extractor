// Claude Skill Extractor — VS Code extension (dependency-free, no build step).
//
// Watches ~/.claude/skill-extractor/state/pending.json. When new candidates
// appear it shows a notification ("N skills discovered"); clicking Review opens
// a webview panel where each candidate can be inspected, edited, installed, or
// rejected-with-a-comment. All actions delegate to engine/review.py so the
// scratch/installed/decisions stores stay consistent.

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const STATE_DIR = path.join(os.homedir(), ".claude", "skill-extractor", "state");
const PENDING_FILE = path.join(STATE_DIR, "pending.json");
const CONFIG_FILE = path.join(os.homedir(), ".claude", "skill-extractor", "extension-config.json");

// Written by install.sh; sane fallbacks if absent.
function loadConfig() {
  const fallback = {
    python: "/opt/homebrew/bin/python3",
    engineDir: path.join(os.homedir(), "Nesh", "skill-extractor", "engine"),
  };
  try {
    return Object.assign(fallback, JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8")));
  } catch (e) {
    return fallback;
  }
}

function review(args) {
  // Run review.py <args...> and parse JSON stdout.
  const cfg = loadConfig();
  return new Promise((resolve, reject) => {
    cp.execFile(
      cfg.python,
      [path.join(cfg.engineDir, "review.py"), ...args],
      { maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err && !stdout) return reject(new Error(stderr || err.message));
        try {
          resolve(JSON.parse(stdout));
        } catch (e) {
          resolve({ raw: stdout, stderr });
        }
      }
    );
  });
}

function readPending() {
  try {
    return JSON.parse(fs.readFileSync(PENDING_FILE, "utf8"));
  } catch (e) {
    return [];
  }
}

let panel = null;

// Ids we've already popped a notification for — persisted across restarts so a
// skill extracted while VS Code was closed still pops up (once) next launch, and
// we never re-nag about one already shown.
function getNotified(context) {
  return new Set(context.globalState.get("notifiedIds", []));
}
function setNotified(context, set) {
  // keep it bounded
  context.globalState.update("notifiedIds", Array.from(set).slice(-5000));
}

function activate(context) {
  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  status.command = "skillExtractor.review";
  context.subscriptions.push(status);
  updateStatus(status, readPending().length);

  context.subscriptions.push(
    vscode.commands.registerCommand("skillExtractor.review", () => openPanel(context))
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("skillExtractor.runNow", () => runMiner())
  );

  const poll = () => {
    const pending = readPending();
    updateStatus(status, pending.length);
    if (!vscode.workspace.getConfiguration("skillExtractor").get("notify", true)) return;

    const notified = getNotified(context);
    const fresh = pending.filter((c) => !notified.has(c.id));
    if (fresh.length) {
      fresh.forEach((c) => notified.add(c.id));
      setNotified(context, notified);
      const label =
        fresh.length === 1
          ? `🎓 New skill discovered from your traces: "${fresh[0].title || fresh[0].name}"`
          : `🎓 ${fresh.length} skills discovered from your Claude Code traces — review & install?`;
      vscode.window.showInformationMessage(label, "Review", "Later").then((choice) => {
        if (choice === "Review") openPanel(context);
      });
    }
    if (panel) refreshPanel();
  };

  const secs = vscode.workspace.getConfiguration("skillExtractor").get("pollSeconds", 45);
  const timer = setInterval(poll, Math.max(10, secs) * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });

  // First look shortly after activation — this is what makes the popup appear
  // right after a reload if there are already-extracted skills pending.
  setTimeout(poll, 2500);
}

function updateStatus(status, n) {
  if (n > 0) {
    status.text = `$(mortar-board) ${n} skill${n === 1 ? "" : "s"}`;
    status.tooltip = "Skill Extractor: click to review discovered skills";
    status.show();
  } else {
    status.text = `$(mortar-board) Skills`;
    status.tooltip = "Skill Extractor: no pending skills";
    status.show();
  }
}

function runMiner() {
  const cfg = loadConfig();
  vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Mining skills from traces…" },
    () =>
      new Promise((resolve) => {
        cp.execFile(
          cfg.python,
          [path.join(cfg.engineDir, "extractor.py")],
          { maxBuffer: 8 * 1024 * 1024 },
          (err) => {
            if (err) vscode.window.showWarningMessage("Skill miner error: " + err.message);
            resolve();
          }
        );
      })
  );
}

function openPanel(context) {
  if (panel) {
    panel.reveal(vscode.ViewColumn.Active);
    refreshPanel();
    return;
  }
  panel = vscode.window.createWebviewPanel(
    "skillExtractorReview",
    "Discovered Skills",
    vscode.ViewColumn.Active,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  panel.onDidDispose(() => (panel = null));
  panel.webview.onDidReceiveMessage(async (msg) => {
    try {
      if (msg.type === "install") {
        const { tmpDir, tmp } = writeEdits(msg.edits);
        try {
          let res = await review(["install", msg.id, "--edits", tmp, "--comment", msg.comment || ""]);
          if (res && res.error === "risky") {
            // The risk lint flagged instruction patterns: require an explicit,
            // modal acknowledgement before the skill becomes a live instruction.
            const choice = await vscode.window.showWarningMessage(
              `This skill body was flagged by the risk lint: ${(res.risk || []).join(", ")}. ` +
              "Installing makes it a persistent agent instruction.",
              { modal: true },
              "Install anyway"
            );
            if (choice !== "Install anyway") {
              refreshPanel();
              return;
            }
            res = await review(["install", msg.id, "--edits", tmp,
                                "--comment", msg.comment || "", "--acknowledge-risk"]);
          }
          vscode.window.showInformationMessage(`Installed skill: ${res.name} → ${res.path}`);
          refreshPanel();
        } finally {
          // Edits can contain the (transcript-derived) skill body: always clean
          // up, even when review.py fails mid-way.
          try { fs.unlinkSync(tmp); fs.rmdirSync(tmpDir); } catch (e) { /* best-effort */ }
        }
      } else if (msg.type === "reject") {
        await review(["reject", msg.id, "--comment", msg.comment || ""]);
        vscode.window.showInformationMessage("Skill rejected (kept in scratch for future learning).");
        refreshPanel();
      } else if (msg.type === "refresh") {
        refreshPanel();
      }
    } catch (e) {
      vscode.window.showErrorMessage("Skill Extractor: " + e.message);
    }
  });
  panel.webview.html = renderHtml([]);
  refreshPanel();
}

function writeEdits(edits) {
  // Unpredictable private dir + 0600 file: the edited body is transcript-derived.
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "skill-edit-"));
  const tmp = path.join(tmpDir, "edits.json");
  fs.writeFileSync(tmp, JSON.stringify(edits || {}), { mode: 0o600 });
  return { tmpDir, tmp };
}

async function refreshPanel() {
  if (!panel) return;
  let items = [];
  try {
    items = await review(["export-pending"]);
  } catch (e) {
    items = readPending();
  }
  panel.webview.postMessage({ type: "data", items });
}

function renderHtml() {
  // The webview is a self-contained SPA; data arrives via postMessage.
  // Nonce-based CSP: only our own script block runs; no remote loads, no
  // inline event handlers (delegation via addEventListener below).
  const nonce = require("crypto").randomBytes(16).toString("hex");
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { font-family: var(--vscode-font-family); color: var(--vscode-foreground);
         padding: 16px; font-size: 13px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: var(--vscode-descriptionForeground); margin-bottom: 16px; }
  .card { border: 1px solid var(--vscode-panel-border); border-radius: 8px;
          padding: 14px 16px; margin-bottom: 16px; background: var(--vscode-editorWidget-background); }
  .title { font-size: 15px; font-weight: 600; }
  .meta { display: flex; gap: 14px; flex-wrap: wrap; margin: 8px 0 10px; align-items:center; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px;
           background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); }
  .outcome-success { background:#1e7f4b; color:#fff; }
  .outcome-failure { background:#8b2e2e; color:#fff; }
  .outcome-meh { background:#7a6a1f; color:#fff; }
  .cat { background:#3b4a8f; color:#fff; text-transform:uppercase; letter-spacing:.03em; }
  .bar { display:inline-block; width:90px; height:8px; border-radius:4px;
         background: var(--vscode-input-background); overflow:hidden; vertical-align:middle; }
  .bar > i { display:block; height:100%; background: var(--vscode-progressBar-background); }
  label { display:block; font-size:11px; text-transform:uppercase; letter-spacing:.04em;
          color: var(--vscode-descriptionForeground); margin:10px 0 3px; }
  input, textarea { width:100%; box-sizing:border-box; font-family:inherit; font-size:13px;
          background: var(--vscode-input-background); color: var(--vscode-input-foreground);
          border:1px solid var(--vscode-input-border,transparent); border-radius:4px; padding:6px 8px; }
  textarea { min-height:120px; resize:vertical; font-family: var(--vscode-editor-font-family), monospace; }
  .row { display:flex; gap:10px; margin-top:12px; align-items:center; }
  button { font-size:13px; padding:6px 14px; border:none; border-radius:4px; cursor:pointer; }
  .install { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  .reject  { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
  .empty { color: var(--vscode-descriptionForeground); padding:40px 0; text-align:center; }
  details { margin-top:8px; } summary { cursor:pointer; color: var(--vscode-textLink-foreground); }
  .why { font-size:12px; color: var(--vscode-descriptionForeground); margin-top:6px; }
  .risk { font-size:12px; margin-top:8px; padding:6px 10px; border-radius:4px;
          background: var(--vscode-inputValidation-warningBackground, #7a5c00);
          border: 1px solid var(--vscode-inputValidation-warningBorder, #b58900); }
</style></head><body>
<h1>🎓 Discovered Skills</h1>
<div class="sub">Mined from your Claude Code traces. Review each candidate, edit if needed, then install or reject. Rejections are kept (with your comment) so mining improves over time.</div>
<div id="list"><div class="empty">Loading…</div></div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
function pct(x){ return Math.round((x||0)*100); }
function esc(s){ return (s||"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function outcomeClass(o){ return "outcome-" + (["success","failure","meh"].includes(o)?o:"meh"); }
function render(items){
  const list = document.getElementById("list");
  if(!items.length){ list.innerHTML = '<div class="empty">No pending skills. New ones will appear here as the miner runs.</div>'; return; }
  list.innerHTML = items.map(c => {
    const s = c.score || {};
    return \`<div class="card" data-id="\${c.id}">
      <div class="title">\${esc(c.title||c.name)}</div>
      <div class="meta">
        <span class="badge cat">\${esc(c.category||"technique")}</span>
        <span class="badge \${outcomeClass(c.trace_outcome)}">trace: \${esc(c.trace_outcome||"?")}</span>
        <span>confidence <span class="bar"><i style="width:\${pct(s.confidence)}%"></i></span> \${pct(s.confidence)}%</span>
        <span>utility <span class="bar"><i style="width:\${pct(s.utility)}%"></i></span> \${pct(s.utility)}%</span>
        <span class="badge">from \${esc((c.source||{}).project||"?")}</span>
      </div>
      \${c.outcome_reason ? '<div class="why">Why this score: '+esc(c.outcome_reason)+'</div>' : ''}
      \${(c.risk||[]).length ? '<div class="risk">⚠ risk lint: '+c.risk.map(esc).join(", ")+' — installing will require an explicit acknowledgement</div>' : ''}
      <label>Name (skill id)</label><input class="f-name" value="\${esc(c.name)}">
      <label>Description (frontmatter — what & when)</label><input class="f-desc" value="\${esc(c.description)}">
      <label>Trigger (when to use)</label><input class="f-trigger" value="\${esc(c.trigger)}">
      <label>Body (the procedure)</label><textarea class="f-body">\${esc(c.body)}</textarea>
      <label>Comment (why you're installing / rejecting — optional)</label><input class="f-comment" placeholder="e.g. useful but renamed; or: too project-specific">
      <div class="row">
        <button class="install" data-act="install" data-id="\${esc(c.id)}">Install skill</button>
        <button class="reject" data-act="reject" data-id="\${esc(c.id)}">Reject</button>
      </div>
    </div>\`;
  }).join("");
}
function act(type,id){
  const card = document.querySelector('.card[data-id="'+id+'"]');
  const g = s => card.querySelector(s).value;
  const edits = { name:g('.f-name'), description:g('.f-desc'), trigger:g('.f-trigger'), body:g('.f-body') };
  vscode.postMessage({ type, id, edits, comment: g('.f-comment') });
}
// Event delegation instead of inline onclick (CSP forbids inline handlers).
document.getElementById("list").addEventListener("click", e => {
  const btn = e.target.closest("button[data-act]");
  if (btn) act(btn.dataset.act, btn.dataset.id);
});
window.addEventListener("message", e => { if(e.data.type==="data") render(e.data.items); });
vscode.postMessage({ type:"refresh" });
</script></body></html>`;
}

function deactivate() {}

module.exports = { activate, deactivate };
