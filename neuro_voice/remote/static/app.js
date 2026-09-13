"use strict";

// 遠隔ランチャー PWA クライアント。
// - 認証: /api/login で HttpOnly Cookie を取得。以後は Cookie で認証される。
// - CSRF: 状態変更POSTには X-Requested-With ヘッダを付ける。
// - 長時間処理: 202 の operation_id を受け取り /api/operations/{id} をポーリング。
// - オフライン: 状態を取得できない時は古い状態を現在として見せない。

const $ = (id) => document.getElementById(id);
const CSRF = { "X-Requested-With": "NeuroLauncher" };

let polling = null;
let busy = false;        // 操作中はボタンを無効化して二重タップを防ぐ
let channelsLoaded = false;

// ---------- API 呼び出し ----------

async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin", headers: CSRF });
  if (res.status === 401) throw { unauth: true };
  if (res.status === 429) throw { rate: true };
  return { status: res.status, body: await res.json().catch(() => ({})) };
}

async function apiPost(path, data) {
  const res = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...CSRF },
    body: JSON.stringify(data || {}),
  });
  if (res.status === 401) throw { unauth: true };
  if (res.status === 429) throw { rate: true };
  return { status: res.status, body: await res.json().catch(() => ({})) };
}

// 202(operation)を受けたら完了までポーリングする
async function runOperation(path, data) {
  const { status, body } = await apiPost(path, data);
  if (status !== 202 || !body.operation_id) {
    // 即時応答(冪等/エラー)
    return body;
  }
  const id = body.operation_id;
  for (let i = 0; i < 90; i++) {
    await sleep(1000);
    const r = await apiGet("/api/operations/" + encodeURIComponent(id));
    if (r.body.state === "SUCCEEDED" || r.body.state === "FAILED") {
      return r.body.result || { ok: r.body.state === "SUCCEEDED" };
    }
  }
  return { ok: false, message: "処理がタイムアウトしました" };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- 画面遷移 ----------

function showLogin(message) {
  stopPolling();
  $("mainView").classList.add("hidden");
  $("loginView").classList.remove("hidden");
  const err = $("loginError");
  if (message) { err.textContent = message; err.classList.remove("hidden"); }
  else err.classList.add("hidden");
}

function showMain() {
  $("loginView").classList.add("hidden");
  $("mainView").classList.remove("hidden");
  startPolling();
  if (!channelsLoaded) loadChannels();
}

// ---------- 状態表示 ----------

const STATE_LABEL = {
  STOPPED: ["off", "停止中"], STARTING: ["busy", "起動中…"], RUNNING: ["on", "起動中"],
  STOPPING: ["busy", "終了中…"], ERROR: ["err", "エラー"],
  DISCONNECTED: ["off", "未接続"], CONNECTING: ["busy", "接続中…"],
  CONNECTED: ["on", "接続中"], DISCONNECTING: ["busy", "退出中…"],
};

function badge(state) {
  const [cls, label] = STATE_LABEL[state] || ["off", state || "-"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function renderStatus(s) {
  $("offlineBanner").classList.add("hidden");
  $("stLauncher").innerHTML = badge(s.launcher_running ? "CONNECTED" : "DISCONNECTED");
  $("stLauncher").firstChild.textContent = s.launcher_running ? "稼働中" : "停止";
  $("stApp").innerHTML = badge(s.app_state);
  $("stDiscord").innerHTML = badge(s.discord_state);
  $("stGuild").textContent = s.guild_name || "-";
  $("stChannel").textContent = s.channel_name || "-";
  $("stOp").textContent = s.operation_in_progress ? (s.current_operation || "実行中") : "なし";
  $("stLastOp").textContent = s.last_operation || "-";
  $("stLastErr").textContent = s.last_error || "-";
  $("stUpdated").textContent = new Date().toLocaleTimeString("ja-JP");

  // オーブの色
  const orb = $("statusOrb");
  orb.className = "orb " +
    (s.operation_in_progress ? "busy" :
     s.app_state === "ERROR" ? "err" :
     s.app_state === "RUNNING" ? "running" : "off");

  // 操作中はワンタッチ系を無効化 (二重タップ防止)
  const lock = busy || s.operation_in_progress;
  $("startJoinBtn").disabled = lock;
  $("leaveStopBtn").disabled = lock;
  document.querySelectorAll(".advanced .btn").forEach((b) => (b.disabled = lock));
}

function showOffline() {
  $("offlineBanner").classList.remove("hidden");
  $("statusOrb").className = "orb off";
  $("stUpdated").textContent = new Date().toLocaleTimeString("ja-JP") + " (取得失敗)";
}

async function refreshStatus() {
  try {
    const { body } = await apiGet("/api/status");
    renderStatus(body);
    // システム設定を開いている間は、起動/終了に追従して実稼働表示を同期する
    const sys = $("systemSection");
    if (sys && sys.open) loadSystem();
  } catch (e) {
    if (e && e.unauth) return showLogin("セッションが切れました。再ログインしてください。");
    showOffline();
  }
}

function startPolling() {
  refreshStatus();
  stopPolling();
  polling = setInterval(refreshStatus, 4000);
}
function stopPolling() { if (polling) { clearInterval(polling); polling = null; } }

// ---------- チャンネル一覧 ----------

async function loadChannels() {
  try {
    const { body } = await apiGet("/api/discord/channels");
    const sel = $("channelSel");
    const current = sel.value;
    sel.innerHTML = '<option value="">(既定 / 自動)</option>';
    (body.channels || []).forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name + (c.members ? ` (${c.members}人)` : "");
      sel.appendChild(opt);
    });
    sel.value = current;
    channelsLoaded = (body.channels || []).length > 0;
    if (!body.ok && body.message) toast(body.message, false);
  } catch (e) {
    if (e && e.unauth) showLogin();
  }
}

// ---------- ペルソナ / 話者 / こころ ----------

async function loadPersonas() {
  try {
    const { body } = await apiGet("/api/personas");
    const sel = $("personaSel");
    sel.innerHTML = "";
    (body.personas || []).forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.key;
      opt.textContent = p.name + (p.character ? "  (" + p.character + ")" : "");
      if (p.active) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!(body.personas || []).length) sel.innerHTML = '<option value="">-</option>';
  } catch (e) { if (e && e.unauth) showLogin(); }
}

async function loadSpeakers() {
  try {
    const { body } = await apiGet("/api/speakers");
    const sel = $("speakerSel");
    sel.innerHTML = "";
    (body.speakers || []).forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.label || s.id;
      if (String(s.id) === String(body.current)) opt.selected = true;
      sel.appendChild(opt);
    });
    if (!(body.speakers || []).length) sel.innerHTML = '<option value="">-</option>';
  } catch (e) { if (e && e.unauth) showLogin(); }
}

// ---------- システム設定 (STT / TTS) ----------

function markSeg(container, matchAttr, value) {
  container.querySelectorAll("button").forEach((b) => {
    b.classList.toggle("active", b.dataset[matchAttr] === String(value));
  });
}

function renderStt(s) {
  s = s || {};
  const el = $("sttActual");
  // 応答が使えない(ok:false=古いランチャー/未起動エラー等)ときは選択を勝手に戻さない。
  // 押した選択を据え置き、実稼働欄だけ注意表示にする。
  const usable = s.ok !== false && (s.requested != null || s.device != null || s.offline);
  if (!usable) {
    el.textContent = "取得できません";
    el.classList.remove("good", "bad");
    $("sttWarn").classList.add("hidden");
    return;
  }
  // セグメントは常に「選択値」を色付け(起動前でも起動後でも押した方が光る)
  const req = (s.requested || "").startsWith("cuda") ? "cuda" : "cpu";
  markSeg($("sttSeg"), "dev", req);
  prefSet("nv_stt_dev", req);
  if (s.offline) {
    // 起動前: 実稼働はまだ無い。選択を保存しただけの状態。
    el.textContent = "起動前 (選択のみ)";
    el.classList.remove("good", "bad");
    $("sttModel").textContent = "";
    $("sttWarn").classList.add("hidden");
    return;
  }
  const actual = (s.device || s.requested || "-").toString().toUpperCase();
  el.textContent = actual.startsWith("CUDA") ? "GPU" : actual;
  el.classList.toggle("bad", s.fell_back === true);
  el.classList.toggle("good", actual.startsWith("CUDA"));
  $("sttModel").textContent = s.model ? "モデル: " + s.model : "";
  $("sttWarn").classList.toggle("hidden", !s.fell_back);
}

function renderTts(t) {
  t = t || {};
  // 使えない応答(古いランチャー/エラー)では選択を戻さない
  if (t.ok === false || (t.engine == null && !t.offline)) {
    $("ttsEngineNow").textContent = "取得できません";
    return;
  }
  const vol = Math.round((t.volume != null ? t.volume : 1) * 100);
  $("ttsVol").value = String(Math.max(0, Math.min(150, vol)));
  $("ttsVolLabel").textContent = vol + "%";
  prefSet("nv_tts_vol", vol);
  const eng = t.engine === "style_bert_vits2" || t.engine === "sbv2" ? "style_bert_vits2"
            : t.engine === "voicevox" ? "voicevox" : "";
  markSeg($("ttsSeg"), "eng", eng);
  if (eng) prefSet("nv_tts_eng", eng);
  let name = eng === "style_bert_vits2" ? "Style-Bert-VITS2"
           : eng === "voicevox" ? "VOICEVOX" : "-";
  // SBV2はGPU/CPUを併記(本当にGPUで動いているか一目で分かるように)
  if (eng === "style_bert_vits2" && t.device) {
    name += t.on_gpu ? " · GPU" : " · CPU";
  }
  $("ttsEngineNow").textContent = t.offline ? name + " (起動時)" : name;
}

// 端末に最後の選択を記憶し、リロード直後(サーバー取得前)でも即座に反映する
function prefGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function prefSet(k, v) { try { localStorage.setItem(k, String(v)); } catch (_) {} }

function applySavedSystemPrefs() {
  const dev = prefGet("nv_stt_dev");
  if (dev) markSeg($("sttSeg"), "dev", dev);
  const eng = prefGet("nv_tts_eng");
  if (eng) {
    markSeg($("ttsSeg"), "eng", eng);
    $("ttsEngineNow").textContent = eng === "style_bert_vits2" ? "Style-Bert-VITS2" : "VOICEVOX";
  }
  // 音量は初期から%を表示する(未保存ならスライダー既定値=100%)
  let vol = prefGet("nv_tts_vol");
  if (vol == null || vol === "") vol = $("ttsVol").value;
  $("ttsVol").value = String(vol);
  $("ttsVolLabel").textContent = Math.round(Number(vol)) + "%";
}

function renderRuntime(r) {
  r = r || {};
  const stt = r.stt || {}, tts = r.tts || {}, llm = r.llm || {};
  const dev = (on, d, fell) => fell ? "CPU ⚠GPU失敗" : (on ? "GPU" : (d ? "CPU" : "?"));
  $("rtStt").textContent = dev(stt.on_gpu, stt.device, stt.fell_back);
  const eng = tts.engine === "style_bert_vits2" ? "SBV2" : tts.engine === "voicevox" ? "VOICEVOX" : (tts.engine || "-");
  $("rtTts").textContent = tts.engine === "style_bert_vits2"
    ? eng + " / " + dev(tts.on_gpu, tts.device) : eng;
  let llmTxt = llm.model || "-";
  if (llm.gpu_percent != null) {
    llmTxt += " (GPU" + llm.gpu_percent + "%" + (llm.fully_gpu ? "" : " ⚠CPU退避") + ")";
  }
  $("rtLlm").textContent = llmTxt;
  const g = r.gpu || {};
  const gEl = $("rtGpu");
  if (g.vram_total_mb) {
    gEl.textContent = `${g.vram_used_mb}/${g.vram_total_mb}MB (${g.vram_percent}%)`
      + (g.vram_tight ? " ⚠満杯" : "");
    gEl.classList.toggle("bad", !!g.vram_tight);
  } else {
    gEl.textContent = "-";
    gEl.classList.remove("bad");
  }
}

let systemLoaded = false;
async function loadSystem() {
  try {
    const [stt, tts, rt] = await Promise.all([
      apiGet("/api/stt"), apiGet("/api/tts"), apiGet("/api/runtime"),
    ]);
    renderStt(stt.body || {});
    renderTts(tts.body || {});
    renderRuntime(rt.body || {});
    systemLoaded = true;
  } catch (e) { if (e && e.unauth) showLogin(); }
}

function bar(value) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return '<div class="bar"><span style="width:' + pct + '%"></span></div>';
}

function renderMind(m) {
  $("mindName").textContent = (m.name || "こころ") + " のこころ";
  if (m.enabled === false) {
    $("mindBody").innerHTML = '<p class="hint left">こころ機能は無効か、取得できませんでした。</p>';
    return;
  }
  const parts = [];
  const rel = m.relationship || {};
  if (rel.level != null) {
    parts.push('<div class="section-title">なかよし度</div>');
    parts.push('<div class="kv"><span class="k">Lv.' + rel.level + " " + esc(rel.label || "") +
      '</span><span class="v">' + (rel.days != null ? rel.days + "日" : "") + "</span></div>");
    if (rel.progress != null) parts.push(bar(rel.progress));
  }
  const mood = m.mood || {};
  if (mood.label) {
    parts.push('<div class="section-title">いまの気分</div>');
    parts.push('<div class="kv"><span class="k">' + esc(mood.emoji || "") + " " +
      esc(mood.label) + '</span><span class="v">valence ' +
      (mood.valence != null ? mood.valence.toFixed(2) : "-") + "</span></div>");
  }
  if (Array.isArray(m.traits) && m.traits.length) {
    parts.push('<div class="section-title">せいかく</div>');
    m.traits.forEach((t) => {
      parts.push('<div class="kv"><span class="k">' + esc(t.label || t.key) +
        '</span><span class="v">' + Math.round((t.value || 0) * 100) + "%</span></div>");
      parts.push(bar(t.value || 0));
    });
  }
  if (Array.isArray(m.likes) && m.likes.length) {
    const likes = m.likes.filter((l) => (l.score || 0) > 0).slice(0, 8);
    const dis = m.likes.filter((l) => (l.score || 0) < 0).slice(0, 6);
    if (likes.length || dis.length) {
      parts.push('<div class="section-title">すき / きらい</div><div class="chips">');
      likes.forEach((l) => parts.push('<span class="chip like">' + esc(l.topic) + "</span>"));
      dis.forEach((l) => parts.push('<span class="chip dislike">' + esc(l.topic) + "</span>"));
      parts.push("</div>");
    }
  }
  const mem = m.memory || {};
  parts.push('<div class="section-title">きおく</div>');
  parts.push('<div class="kv"><span class="k">全' + (mem.total || 0) + "件</span><span class=\"v\">" +
    "事実" + (mem.facts || 0) + " / 好み" + (mem.preferences || 0) + " / 思い出" + (mem.episodes || 0) +
    "</span></div>");
  if (Array.isArray(m.recent_memories) && m.recent_memories.length) {
    m.recent_memories.slice(0, 6).forEach((r) => {
      const t = typeof r === "string" ? r : (r.text || "");
      if (t) parts.push('<div class="mem">・' + esc(t) + "</div>");
    });
  }
  if (mem.session_summary) {
    parts.push('<div class="section-title">この会話のこれまで</div>');
    parts.push('<p class="mem">' + esc(mem.session_summary) + "</p>");
  }
  $("mindBody").innerHTML = parts.join("");
}

async function openMind() {
  $("mindBody").innerHTML = '<p class="hint left">読み込み中…</p>';
  $("mindOverlay").classList.remove("hidden");
  try {
    const { body } = await apiGet("/api/mind");
    renderMind(body);
  } catch (e) {
    if (e && e.unauth) return showLogin();
    $("mindBody").innerHTML = '<p class="hint left">こころステータスを取得できませんでした。</p>';
  }
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- トースト ----------

let toastTimer = null;
function toast(msg, ok) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast " + (ok ? "ok" : "err");
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4200);
}

// ---------- 操作 ----------

async function withBusy(label, fn) {
  if (busy) return;
  busy = true;
  document.querySelectorAll(".actions .btn, .advanced .btn").forEach((b) => (b.disabled = true));
  try {
    const result = await fn();
    const ok = result && result.ok !== false;
    toast((result && result.message) || (ok ? label + "が完了しました" : label + "に失敗しました"), ok);
  } catch (e) {
    if (e && e.unauth) return showLogin("セッションが切れました。再ログインしてください。");
    if (e && e.rate) toast("リクエストが多すぎます。少し待ってから再試行してください。", false);
    else toast("通信に失敗しました", false);
  } finally {
    busy = false;
    await refreshStatus();
  }
}

function selectedChannel() { return $("channelSel").value || ""; }

// ---------- 起動 ----------

function bind() {
  $("loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const token = $("tokenInput").value.trim();
    if (!token) return;
    $("loginBtn").disabled = true;
    try {
      const { status } = await apiPost("/api/login", { token });
      if (status === 200) {
        $("tokenInput").value = "";
        showMain();
      } else {
        showLogin("認証に失敗しました。トークンを確認してください。");
      }
    } catch (e) {
      // 401=トークン不一致 / 429=レート制限 / それ以外=接続不可、を区別する
      if (e && e.unauth) {
        showLogin("トークンが一致しません。.env の REMOTE_LAUNCHER_ADMIN_TOKEN と同じ値か、前後の空白が混ざっていないか確認してください。");
      } else if (e && e.rate) {
        showLogin("リクエストが多すぎます。1分ほど待ってから再試行してください。");
      } else {
        showLogin("ランチャーに接続できません。Tailscale接続を確認してください。");
      }
    } finally {
      $("loginBtn").disabled = false;
    }
  });

  $("logoutBtn").addEventListener("click", async () => {
    try { await apiPost("/api/logout", {}); } catch (_) {}
    showLogin();
  });

  $("refreshBtn").addEventListener("click", () => { refreshStatus(); loadChannels(); });
  $("refreshChBtn").addEventListener("click", loadChannels);

  $("startJoinBtn").addEventListener("click", () =>
    withBusy("起動して参加", () =>
      runOperation("/api/actions/start-and-join", { channel_id: selectedChannel() })));

  $("leaveStopBtn").addEventListener("click", () => {
    if (!confirm("Discordから退出し、AIアシスタントを終了します。よろしいですか？")) return;
    withBusy("退出して終了", () => runOperation("/api/actions/leave-and-stop", {}));
  });

  document.querySelectorAll(".advanced .btn[data-act]").forEach((b) => {
    b.addEventListener("click", () => {
      const act = b.dataset.act;
      const map = {
        "app-start": ["本体起動", () => runOperation("/api/app/start", {})],
        "app-stop": ["本体終了", () => runOperation("/api/app/stop", {})],
        "join": ["VC参加", () => runOperation("/api/discord/join", { channel_id: selectedChannel() })],
        "leave": ["VC退出", () => runOperation("/api/discord/leave", {})],
      };
      const [label, fn] = map[act];
      withBusy(label, fn);
    });
  });

  // AI設定セクションを開いたらペルソナ・話者を読み込む
  $("aiSection").addEventListener("toggle", (e) => {
    if (e.target.open) { loadPersonas(); loadSpeakers(); }
  });
  $("applyPersonaBtn").addEventListener("click", () => {
    const key = $("personaSel").value;
    if (!key) return;
    withBusy("ペルソナ切替", async () => {
      const { body } = await apiPost("/api/persona", { key });
      return body;
    }).then(() => loadSpeakers());  // 話者もペルソナ既定へ更新される
  });
  $("applySpeakerBtn").addEventListener("click", () => {
    const id = $("speakerSel").value;
    if (!id) return;
    withBusy("話者変更", async () => {
      const { body } = await apiPost("/api/speaker", { speaker_id: id });
      return body;
    });
  });
  // システム設定セクションを開いたらSTT/TTSの現在値を読み込む
  $("systemSection").addEventListener("toggle", (e) => {
    if (e.target.open) loadSystem();
  });
  $("sttSeg").querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      const device = b.dataset.dev;
      if (b.classList.contains("active")) return;  // 既にその設定
      markSeg($("sttSeg"), "dev", device);  // 即座に選択を反映(応答を待たない)
      prefSet("nv_stt_dev", device);
      withBusy("音声認識の切替", async () => {
        const { body } = await apiPost("/api/stt/device", { device });
        return body;
      }).then(loadSystem);
    });
  });
  // 音量: つまみ操作中はラベルだけ更新、離した(change)時に反映
  $("ttsVol").addEventListener("input", () => {
    $("ttsVolLabel").textContent = $("ttsVol").value + "%";
  });
  $("ttsVol").addEventListener("change", () => {
    prefSet("nv_tts_vol", $("ttsVol").value);
    const volume = Math.max(0, Math.min(2, Number($("ttsVol").value) / 100));
    withBusy("音量変更", async () => {
      const { body } = await apiPost("/api/tts/volume", { volume });
      return body;
    });
  });
  $("ttsSeg").querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      const engine = b.dataset.eng;
      if (b.classList.contains("active")) return;
      markSeg($("ttsSeg"), "eng", engine);  // 即座に選択を反映
      prefSet("nv_tts_eng", engine);
      $("ttsEngineNow").textContent = engine === "style_bert_vits2" ? "Style-Bert-VITS2" : "VOICEVOX";
      withBusy("読み上げエンジン切替", async () => {
        const { body } = await apiPost("/api/tts/engine", { engine });
        return body;
      }).then(loadSystem);
    });
  });

  $("mindBtn").addEventListener("click", openMind);
  $("mindClose").addEventListener("click", () => $("mindOverlay").classList.add("hidden"));
  $("mindOverlay").addEventListener("click", (e) => {
    if (e.target === $("mindOverlay")) $("mindOverlay").classList.add("hidden");
  });
}

// ---------- 初期化 ----------

async function init() {
  bind();
  applySavedSystemPrefs();  // 端末に記憶した最後の選択を即反映(音量%も初期表示)
  // Cookieが生きていればそのままメイン画面へ
  try {
    const { status } = await apiGet("/api/status");
    if (status === 200) showMain(); else showLogin();
  } catch (e) {
    if (e && e.unauth) showLogin();
    else showLogin(); // オフラインでもログイン画面は出す
  }
  // Service Worker: 登録し、新バージョンが来たら自動でリロードして最新JS/CSSに更新する
  // (これ以降は「古いapp.jsが残る」キャッシュ不整合が自動解消される)
  if ("serviceWorker" in navigator) {
    let refreshing = false;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (refreshing) return;
      refreshing = true;
      location.reload();
    });
    navigator.serviceWorker.register("/sw.js").then((reg) => {
      reg.update().catch(() => {});
    }).catch(() => {});
  }
}

document.addEventListener("DOMContentLoaded", init);
