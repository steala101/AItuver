"use strict";

// Local-only DAVE voice bridge.  Discord cryptography and Opus decoding stay
// in this process; Python receives only already-decoded PCM plus transport
// metadata.  Never expose this server beyond localhost.
const { PassThrough } = require("node:stream");
const crypto = require("node:crypto");
const Dysnomia = require("@projectdysnomia/dysnomia");
const { WebSocketServer } = require("ws");

const PORT = Number(process.env.AITUBER_DAVE_PORT || 18765);
const SECRET = process.env.AITUBER_DAVE_SECRET || "";
const TOKEN = process.env.DISCORD_BOT_TOKEN || "";

if (!SECRET || !TOKEN) {
  throw new Error("AITUBER_DAVE_SECRET and DISCORD_BOT_TOKEN are required");
}

let peer = null;
let voice = null;
let playback = null;
let currentGuildId = null;
let currentChannelId = null;
let botReady = false;
let fatalShutdownScheduled = false;

function sendJson(message) {
  if (peer?.readyState !== 1) return;
  try {
    peer.send(JSON.stringify(message));
  } catch (error) {
    // A Python reconnect may close the local IPC while a Discord voice event
    // is being delivered.  Never let that terminate the DAVE process.
    console.error(`[DAVE] local IPC send failed: ${error.message}`);
  }
}

function log(level, message, extra = {}) {
  sendJson({ type: "status", level, message, ...extra });
  // Do not log tokens, secrets, or PCM.
  console[level === "error" ? "error" : "log"](`[DAVE] ${message}`);
}

function accountName(userId) {
  const guild = currentGuildId && bot.guilds.get(currentGuildId);
  const member = guild?.members.get(userId);
  return member?.nick || member?.user?.globalName || member?.user?.username || userId;
}

function ssrcFor(userId) {
  if (!voice?.ssrcUserMap) return null;
  for (const [ssrc, id] of Object.entries(voice.ssrcUserMap)) {
    if (id === userId) return Number(ssrc);
  }
  return null;
}

function sendPcm(pcm, userId, timestamp, sequence) {
  try {
    if (peer?.readyState !== 1 || !userId || !Buffer.isBuffer(pcm)) return;
    const header = Buffer.from(JSON.stringify({
      type: "pcm",
      source: "discord_direct",
      source_account: { id: String(userId), name: accountName(userId) },
      guild_id: currentGuildId,
      channel_id: currentChannelId,
      ssrc: ssrcFor(userId),
      timestamp,
      sequence,
      sample_rate: 48000,
      channels: 2,
      encoding: "pcm_s16le",
    }));
    const prefix = Buffer.allocUnsafe(4);
    prefix.writeUInt32BE(header.length, 0);
    peer.send(Buffer.concat([prefix, header, pcm]), { binary: true });
  } catch (error) {
    log("error", `direct PCM forwarding failed: ${error.message}`);
  }
}

function attachReceiver(connection) {
  connection.removeAllListeners("warn");
  connection.on("warn", (message) => log("warn", String(message)));
  connection.on("error", (error) => log("error", `voice error: ${error.message}`));
  connection.on("disconnect", (error) => {
    log("warn", `voice disconnected${error ? `: ${error.message}` : ""}`);
    voice = null;
    playback = null;
  });
  connection.on("ready", () => log("info", "DAVE voice session ready", {
    dave_enabled: connection.daveEnabled,
    dave_protocol_version: connection.daveProtocolVersion || null,
  }));
  connection.receive("pcm").on("data", sendPcm);
}

// Discord voice is clocked in 20 ms Opus frames.  Keeping the input stream
// open across several TTS sentences is intentional, but routing it through
// FFmpeg made the stream susceptible to an under-run at sentence boundaries:
// FFmpeg could buffer/restart while the voice connection was still consuming
// frames.  Feed already-normalized 48 kHz stereo PCM directly to Dysnomia's
// PCM -> Opus transformer instead.
const PCM_FRAME_BYTES = 3840; // 20 ms, 48 kHz, stereo, s16le

function ensurePlayback(connection) {
  if (playback) return playback;
  playback = {
    stream: new PassThrough({ highWaterMark: 2 * 1024 * 1024 }),
    remainder: Buffer.alloc(0),
    framesWritten: 0,
  };
  playback.stream.on("error", (error) => log("error", `playback pipe error: ${error.message}`));
  connection.play(playback.stream, {
    format: "pcm",
    // ``samplingRate`` is Dysnomia's option name (not ``sampleRate``).
    // Explicit frame sizing avoids a library default changing the PCM frame
    // boundary and producing metallic/garbled speech.
    samplingRate: 48000,
    frameDuration: 20,
    frameSize: 960,
    pcmSize: PCM_FRAME_BYTES,
    // Avoid the long-lived FFmpeg pipe.  inlineVolume uses the direct PCM
    // transformer and is only used as a stable encoder path; volume remains
    // at its neutral 1.0 setting.
    inlineVolume: true,
    voiceDataTimeout: -1,
  });
  log("info", "direct TTS playback encoder started", { frame_bytes: PCM_FRAME_BYTES });
  return playback;
}

function writePlaybackPcm(connection, pcm) {
  const state = ensurePlayback(connection);
  const combined = state.remainder.length
    ? Buffer.concat([state.remainder, pcm])
    : pcm;
  const completeBytes = combined.length - (combined.length % PCM_FRAME_BYTES);
  if (completeBytes > 0) {
    // Write one exact Discord frame at a time.  This prevents arbitrary
    // WebSocket/TTS chunk boundaries from reaching the Opus encoder.
    for (let offset = 0; offset < completeBytes; offset += PCM_FRAME_BYTES) {
      state.stream.write(combined.subarray(offset, offset + PCM_FRAME_BYTES));
      state.framesWritten += 1;
    }
  }
  state.remainder = completeBytes < combined.length
    ? Buffer.from(combined.subarray(completeBytes))
    : Buffer.alloc(0);
}

function stopPlayback(connection) {
  if (playback?.stream) {
    playback.stream.destroy();
  }
  playback = null;
  // Clear Piper's queued Opus packets as well as the input PCM stream.  This
  // makes a substantive barge-in stop promptly instead of speaking the rest
  // of the previous sentence after Python has cancelled generation.
  connection.stopPlaying();
  log("info", "direct TTS playback interrupted");
}

function pausePlayback(connection) {
  if (!playback) return;
  connection.pause();
  log("info", "direct TTS playback paused");
}

function resumePlayback(connection) {
  if (!playback) return;
  connection.resume();
  log("info", "direct TTS playback resumed");
}

async function join(guildId, channelId) {
  if (!botReady) throw new Error("Discord gateway is not ready");
  const channel = bot.getChannel(channelId);
  if (!channel?.join) throw new Error("voice channel was not found in Dysnomia cache");
  if (voice && currentGuildId === guildId && currentChannelId === channelId && voice.ready) {
    sendJson({ type: "joined", guild_id: guildId, channel_id: channelId, reused: true });
    return;
  }
  if (voice && currentGuildId) bot.voiceConnections.leave(currentGuildId);
  currentGuildId = String(guildId);
  currentChannelId = String(channelId);
  voice = await channel.join({ daveEncryption: true, selfDeaf: false, selfMute: false });
  attachReceiver(voice);
  sendJson({ type: "joined", guild_id: currentGuildId, channel_id: currentChannelId,
    dave_enabled: voice.daveEnabled, dave_protocol_version: voice.daveProtocolVersion || null });
  log("info", "joined voice channel using direct DAVE receive");
}

async function leave() {
  if (voice && currentGuildId) await Promise.resolve(bot.voiceConnections.leave(currentGuildId));
  voice = null;
  playback = null;
  const oldChannel = currentChannelId;
  currentGuildId = null;
  currentChannelId = null;
  sendJson({ type: "left", channel_id: oldChannel });
}

const bot = new Dysnomia.Client(`Bot ${TOKEN}`, {
  gateway: { intents: ["guilds", "guildVoiceStates"] },
  voice: { daveEncryption: true, opusOnly: false },
});

bot.on("ready", () => {
  botReady = true;
  sendJson({ type: "sidecar_ready", bot_id: bot.user?.id || null });
  log("info", "DAVE receiver logged in", { bot_id: bot.user?.id || null });
});
bot.on("error", (error) => {
  sendJson({ type: "sidecar_error", message: `Discord gateway error: ${error.message}` });
  log("error", `Discord gateway error: ${error.message}`);
});

const server = new WebSocketServer({ host: "127.0.0.1", port: PORT, maxPayload: 8 * 1024 * 1024 });
server.on("listening", () => log("info", `local IPC listening on 127.0.0.1:${PORT}`));
server.on("connection", (socket, request) => {
  if (request.headers["x-aituber-secret"] !== SECRET || peer) {
    socket.close(1008, "unauthorized or already connected");
    return;
  }
  peer = socket;
  sendJson({ type: "hello", protocol: 1, dave_library: "@projectdysnomia/dysnomia" });
  if (botReady) sendJson({ type: "sidecar_ready", bot_id: bot.user?.id || null });
  socket.on("close", () => { if (peer === socket) peer = null; });
  socket.on("error", (error) => log("warn", `local IPC error: ${error.message}`));
  socket.on("message", async (raw, isBinary) => {
    try {
      if (isBinary) return;
      const command = JSON.parse(raw.toString("utf8"));
      if (command.type === "join") await join(String(command.guild_id), String(command.channel_id));
      else if (command.type === "leave") await leave();
      else if (command.type === "play_pcm") {
        if (!voice?.ready) throw new Error("voice session is not ready");
        const pcm = Buffer.from(String(command.pcm_b64 || ""), "base64");
        if (pcm.length) writePlaybackPcm(voice, pcm);
      } else if (command.type === "stop_pcm") {
        if (voice?.ready) stopPlayback(voice);
      } else if (command.type === "pause_pcm") {
        if (voice?.ready) pausePlayback(voice);
      } else if (command.type === "resume_pcm") {
        if (voice?.ready) resumePlayback(voice);
      } else if (command.type === "ping") sendJson({ type: "pong" });
      else throw new Error(`unsupported command: ${command.type}`);
    } catch (error) {
      log("error", `command failed: ${error.message}`);
      sendJson({ type: "command_error", message: error.message });
    }
  });
});

process.on("SIGINT", () => { void leave(); server.close(); bot.disconnect({ reconnect: false }); });
process.on("SIGTERM", () => { void leave(); server.close(); bot.disconnect({ reconnect: false }); });
function reportFatal(kind, error) {
  const message = `${kind}: ${error?.stack || error?.message || String(error)}`;
  sendJson({ type: "sidecar_fatal", message });
  console.error(`[DAVE] ${message}`);
  if (!fatalShutdownScheduled) {
    fatalShutdownScheduled = true;
    setTimeout(() => process.exit(1), 50).unref();
  }
}
process.on("uncaughtException", (error) => reportFatal("uncaught exception", error));
process.on("unhandledRejection", (error) => reportFatal("unhandled rejection", error));
bot.connect();
