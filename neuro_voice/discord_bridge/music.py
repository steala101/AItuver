"""Discord VC で音楽を流す (YouTube検索 → FFmpeg で VC へ)。

- 「〇〇流して」「音楽かけて」「止めて」「次」を音声コマンドで操作する。
- TTS(ニューロの声)と同じ AudioSource にミックスし、ニューロが喋る間は音楽を
  小さくする(ダッキング)。discord.py の VoiceClient は同時に1ソースしか再生
  できないため、TTSキューと音楽を1本に合成する。

必要: yt-dlp (pip) と FFmpeg (システムにインストール、PATHを通す)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import numpy as np

logger = logging.getLogger(__name__)

_FRAME_BYTES = 3840  # 20ms @ 48kHz stereo int16
_SILENCE = b"\x00" * _FRAME_BYTES
# ニューロ発話中に音楽を下げる割合(通常音量に対する%)。
# 25%だと音楽音量が低い時に発話中ほぼ無音になっていたため、聞こえる範囲に緩めた。
# A voice reply should remain intelligible without making the music vanish.
# This is intentionally a mild duck, not a pause/mute.
_MUSIC_DUCK_PCT = 70


# ---------- 音声コマンド解析 ----------

_RE_PLAY = re.compile(
    r"^(.*?)\s*(?:を|って|でも|とか)?\s*(?:流して|かけて|再生して|ながして|プレイして|かけといて)"
)
_RE_STOP = re.compile(
    r"(?:音楽|曲|再生|それ|これ|今の|BGM)?\s*(?:を)?\s*(?:止めて|停めて|とめて|停止|ストップ|やめて|止めろ)"
)
_RE_SKIP = re.compile(r"(?:次|つぎ|スキップ|skip)(?:の曲|の|に|して|お願い)?")
_RE_CLEAR_QUEUE = re.compile(
    r"(?:音楽|曲|再生|プレイリスト)?(?:の)?"
    r"(?:キュー|予約(?:曲)?|待機中(?:の曲)?)"
    r"(?:に(?:溜ま(?:っ)?た|たまった))?(?:曲|音楽)?\s*(?:を|は)?\s*"
    r"(?:消して|空にして|クリア(?:して)?|削除(?:して)?|消去(?:して)?)"
)
_GENERIC = {"", "音楽", "なんか", "何か", "曲", "bgm", "適当", "てきとう"}

# ``流して`` / ``止めて`` だけを含む雑談を、即座にプレイヤー操作へ
# 変換しないための判定材料。実際の会話状態は呼出元で渡す。
_EXPLICIT_MUSIC_TARGET = re.compile(
    r"(?:音楽|曲|BGM|再生|キュー|予約(?:曲)?|プレイリスト)", re.IGNORECASE
)
_META_COMMAND_REFERENCE = re.compile(
    r"(?:流して|かけて|再生して|止めて|停めて|とめて|停止|ストップ|"
    r"スキップ|消して|クリア)(?:って|と|という|とは|場合|とき|時)",
    re.IGNORECASE,
)
_CURRENT_TRACK_DISCUSSION = re.compile(
    r"(?:今|この|さっき|先ほど|再生中)\s*(?:の)?\s*"
    r"(?:曲|音楽|BGM|やつ).*(?:どう思|感想|好き|聞こえ|聴こえ|良い|いい)",
    re.IGNORECASE,
)
_MUSIC_ACTIONS = frozenset({
    "play", "stop", "skip", "clear_queue", "seek", "seek_abs", "speed", "volume", "volume_set",
})


@dataclass(frozen=True)
class MusicIntentPlan:
    """LLMが提案した、検証前の音楽ツール実行計画。"""

    execute: bool
    action: str | None = None
    query: str | None = None
    reason: str = ""

# 「じゃあそれを再生して」のような、直前の曲指定を参照する表現。
# 曲名そのものが無い時だけ文脈の候補へ解決するため、具体的な検索語を壊さない。
_MUSIC_REFERENCE = re.compile(
    r"^(?:(?:じゃあ|じゃぁ|では|それじゃ|なら|よし|えっと|あの)[、,\s]*)*"
    r"(?:それ|あれ|これ|その(?:曲|やつ)?|あの(?:曲|やつ)?|"
    r"さっきの(?:曲|やつ)?|前の(?:曲|やつ)?|先ほどの(?:曲|やつ)?|やつ)\s*(?:を|に|の)?$",
    re.IGNORECASE,
)
_MUSIC_THEME_CANDIDATE = re.compile(
    r"^(.+?(?:の)?(?:オープニングテーマ|エンディングテーマ|OP|ED|主題歌|挿入歌|テーマ曲|サントラ))"
    r"(?:を|に|が)?(?:して|お願い|おねがい|がいい|にしたい)?[。.!！?？]*$",
    re.IGNORECASE,
)
# 「おまかせ」判定用: これらの一般語・助詞だけで構成された指定は具体的な曲名ではない
# (例: 「適当に音楽」「なんでもいいから」→ AI自身に選曲させる)
_OMAKASE_TOKENS = re.compile(
    r"(?:なんでも|なんか|なにか|何か|適当|てきとう|おまかせ|お任せ|まかせ|"
    r"いい感じ|いいかんじ|いい|よい|好きな|すきな|気分|きぶん|その日|きょう|今日|"
    r"有名|人気|定番|ヒット|おすすめ|オススメ|流行り|流行|はやり|話題|"
    r"曲|きょく|音楽|おんがく|ソング|song|bgm|を|に|の|で|な|や|とか|"
    r"から|くらい|ぐらい|ね|よ|の?やつ|もの|感じ|かんじ)")


def _looks_omakase(query: str) -> bool:
    """具体的な曲名/アーティストを含まない「おまかせ」指定かどうか。"""
    rest = _OMAKASE_TOKENS.sub("", query).strip(" 　、。!?！？")
    # 「1曲」「一曲」は曲名ではなく、再生数の指定。
    rest = re.sub(r"(?:[0-9０-９]+|[一二三])\s*(?:曲|きょく)?", "", rest)
    return rest == ""


def is_music_reference(query: str) -> bool:
    """曲名を持たない指示語だけの再生指定かを返す。"""
    normalized = (query or "").strip().lower().strip(" 。、,.!！?？")
    return bool(_MUSIC_REFERENCE.fullmatch(normalized))


def extract_music_reference_candidate(text: str) -> str | None:
    """次の「それ」に使える、未再生の作品テーマ指定を短く抽出する。"""
    normalized = (text or "").strip()
    match = _MUSIC_THEME_CANDIDATE.fullmatch(normalized)
    return match.group(1).strip() if match else None


def should_execute_music_command(
    text: str,
    action: str,
    *,
    assistant_addressed: bool,
    one_human_conversation: bool,
    active_exchange: bool,
) -> tuple[bool, str]:
    """Return whether a parsed music command is safe to execute now.

    Parsing deliberately stays permissive so that spoken Japanese is accepted.
    Execution is stricter: an explicit music target or a direct call to the
    assistant wins, while a bare ``止めて`` only works in an active 1:1 turn.
    This keeps group-chat and meta-conversation phrases from controlling audio.
    """
    normalized = (text or "").strip()
    if _META_COMMAND_REFERENCE.search(normalized):
        return False, "meta_conversation"
    if normalized.rstrip().endswith(("?", "？")) and not assistant_addressed:
        return False, "question_form"
    if _EXPLICIT_MUSIC_TARGET.search(normalized):
        return True, "explicit_music_target"
    if assistant_addressed:
        return True, "assistant_addressed"
    if one_human_conversation and active_exchange:
        return True, "active_one_to_one_followup"
    return False, "ambiguous_without_context"


def is_current_track_discussion(text: str) -> bool:
    """再生中/直前の曲への感想を、一般的な音楽Web検索と混同しない。"""
    return bool(_CURRENT_TRACK_DISCUSSION.search((text or "").strip()))


async def plan_music_intent(
    llm,
    text: str,
    *,
    parsed_action: str,
    parsed_query: str | None,
    music_playing: bool,
    now_playing: dict | None,
    last_query: str | None,
    conversation_excerpt: list[dict] | None = None,
    timeout_s: float = 5.0,
) -> MusicIntentPlan | None:
    """LLMに候補発話が本当に音楽操作の依頼かを判定させる。"""
    if llm is None or parsed_action not in _MUSIC_ACTIONS:
        return None
    title = str((now_playing or {}).get("title", "") or "なし")
    current_query = str((now_playing or {}).get("query", "") or "なし")
    recent_turns = " / ".join(
        f"{'ユーザー' if item.get('role') == 'user' else 'AI'}: {str(item.get('content', ''))[:180]}"
        for item in (conversation_excerpt or [])[-6:]
        if isinstance(item, dict)
    ) or "なし"
    prompt = [
        {
            "role": "system",
            "content": (
                "あなたは音声AIの音楽ツール実行プランナー。ユーザー発話が実際に音楽を"
                "操作してほしい依頼かを文脈的に判定する。『止めてって言っても止めないで』、"
                "『流してとはどういう意味？』、曲の感想・雑談・質問は execute=false。"
                "実行依頼なら execute=true。action は候補と同じものだけを返す。"
                "play で曲名が曖昧なら、希望する雰囲気を保った短い日本語のYouTube検索語を"
                "query に入れる（特定の実在曲名を勝手に捏造しない）。"
                "JSONだけを返す: {\"execute\":true|false,\"action\":\"候補action\"|null,"
                "\"query\":\"検索語\"|null,\"reason\":\"短い理由\"}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"発話: {text}\n候補action: {parsed_action}\n候補query: {parsed_query or 'なし'}\n"
                f"再生中: {'はい' if music_playing else 'いいえ'}\n"
                f"再生中の曲: {title}\n再生中の検索語: {current_query}\n"
                f"直近に指定された曲: {last_query or 'なし'}\n"
                f"直近の会話: {recent_turns}"
            ),
        },
    ]

    async def _collect() -> str:
        from neuro_voice.utils.textseg import strip_think

        chunks: list[str] = []
        async for token in strip_think(llm.generate(prompt)):
            chunks.append(token)
            if sum(map(len, chunks)) >= 800:
                break
        return "".join(chunks)[:800]

    try:
        raw = await asyncio.wait_for(_collect(), timeout=max(0.5, float(timeout_s)))
    except Exception:
        logger.warning("音楽意図プランナーが利用できないため安全な規則判定へフォールバック", exc_info=True)
        return None
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.info("音楽意図プランナーのJSONを読めなかった: %s", raw[:160])
        return None
    execute = bool(data.get("execute", False))
    action = str(data.get("action") or "").strip() or None
    query = str(data.get("query") or "").strip() or None
    reason = str(data.get("reason") or "").strip()
    # LLMは解析済みの具体的操作を承認/却下するだけ。自由なツール呼び出しは許さない。
    if execute and action != parsed_action:
        logger.warning("音楽意図プランのaction不一致を拒否: parsed=%s planned=%s", parsed_action, action)
        return MusicIntentPlan(False, reason="action_mismatch")
    if not execute:
        logger.info("音楽意図プラン: execute=false action=%s reason=%s", action, reason or "llm_declined")
        return MusicIntentPlan(False, reason=reason or "llm_declined")
    logger.info("音楽意図プラン: execute=true action=%s query=%s reason=%s",
                action, query or "(なし)", reason or "llm_confirmed")
    return MusicIntentPlan(True, action=action, query=query, reason=reason or "llm_confirmed")


def now_playing_context(track: dict | None) -> str | None:
    """再生中の曲をペルソナへ共有するための、短い会話コンテキストを作る。"""
    if not track:
        return None
    title = str(track.get("title", "") or "").strip()
    artist = str(track.get("artist", "") or "").strip()
    query = str(track.get("query", "") or "").strip()
    if not title and not query:
        return None
    label = " - ".join(part for part in (artist, title) if part) or query
    query_note = f"（検索指定: {query}）" if query and query != label else ""
    source_note = "AudDが現在の再生音から特定した結果です。" if track.get("source") == "audd" else ""
    return (
        f"【共有中の音楽】いまユーザーとあなたは「{label}」を再生中です{query_note}。\n"
        + source_note
        + "ユーザーが「この曲」「今の曲」について質問したら、この再生中の曲の話として答えること。"
        "曲を聴かせてくれないと分からない、とは返さない。質問されていないのに曲への感想を始めないこと。"
        "ただし、歌詞・特定の演奏箇所など再生情報だけでは確認できない細部は、知ったふりで断定しない。"
    )

# --- 細かい操作 (再生中のみ) ---
_RE_SPEED = re.compile(
    r"(?:(\d+(?:[\.,]\d+)?)\s*倍(?:速)?|倍速)(?:にして|で(?:再生)?|に(?:して)?|して)?"
)
_RE_SPEED_RESET = re.compile(r"(?:等速|通常(?:の)?速度|普通(?:の)?速度|元の速度)(?:に戻して|にして|に|で)?")
_RE_SEEK = re.compile(
    r"(\d+)\s*(分|秒)\s*(?:ほど|くらい)?\s*"
    r"(進めて|飛ばして|送って|スキップ|とばして|戻して|巻き戻して|もどして|バック)"
)
_RE_SEEK_LITTLE = re.compile(r"(?:ちょっと|少し|すこし)\s*(進めて|飛ばして|戻して|巻き戻して)")
_RE_RESTART = re.compile(r"(?:最初|頭|はじめ)から(?:再生|流して|かけて|お願い)?")
# 音量 (「音量/ボリューム/音」を必須にして誤爆を防ぐ)
_RE_VOL_UP = re.compile(
    r"(?:音量|ボリューム|ヴォリューム|音)\s*を?\s*(?:もっと)?\s*"
    r"(?:上げて|あげて|大きくして|大きく|でかくして|でかく)")
_RE_VOL_DOWN = re.compile(
    r"(?:音量|ボリューム|ヴォリューム|音)\s*を?\s*(?:もっと)?\s*"
    r"(?:下げて|さげて|小さくして|小さく|ちいさくして|ちいさく|絞って|しぼって)")
_RE_VOL_MAX = re.compile(r"(?:音量|ボリューム|ヴォリューム)\s*を?\s*(?:最大|マックス|全開|max)")
_RE_VOL_SET = re.compile(r"(?:音楽の?音|音量|ボリューム|ヴォリューム|音)\s*を?\s*(\d{1,3})\s*(?:%|パーセント)")


def parse_music_command(text: str, music_playing: bool) -> tuple[str, str | None] | None:
    """発話テキストから音楽コマンドを判定する。

    返り値: ("play", query|None) / ("stop", None) / ("skip", None) / None
    停止・スキップは誤爆防止のため音楽再生中のみ判定する。
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    # キュー操作は停止中でも意味がある。現在再生している曲は残し、予約分だけ消す。
    if _RE_CLEAR_QUEUE.search(t):
        return ("clear_queue", None)
    if music_playing:
        # 細かい操作を先に判定 (「10秒スキップ」を曲スキップと誤認しないため)
        m = _RE_SEEK.search(t)
        if m:
            n = int(m.group(1)) * (60 if m.group(2) == "分" else 1)
            if m.group(3) in ("戻して", "巻き戻して", "もどして", "バック"):
                n = -n
            return ("seek", n)
        m = _RE_SEEK_LITTLE.search(t)
        if m:
            return ("seek", -10 if "戻" in m.group(1) else 10)
        if _RE_RESTART.search(t):
            return ("seek_abs", 0)
        if _RE_SPEED_RESET.search(t):
            return ("speed", 1.0)
        m = _RE_SPEED.search(t)
        if m:
            try:
                val = float((m.group(1) or "2").replace(",", "."))
            except ValueError:
                val = 2.0
            return ("speed", val)
        # 音量
        if _RE_VOL_MAX.search(t):
            return ("volume_set", 1.5)   # 最大(150%)
        m = _RE_VOL_SET.search(t)
        if m:
            return ("volume_set", max(0, min(150, int(m.group(1)))) / 100.0)
        if _RE_VOL_UP.search(t):
            return ("volume", 0.2)
        if _RE_VOL_DOWN.search(t):
            return ("volume", -0.2)
        if _RE_SKIP.search(t):
            return ("skip", None)
        if _RE_STOP.search(t):
            return ("stop", None)
    m = _RE_PLAY.search(t)
    if m:
        query = m.group(1).strip(" 　、。を")
        # 先頭の呼びかけ・フィラーを除去 (「ニューロ 〇〇流して」等)
        query = re.sub(
            r"^(?:(?:よし|えーと|えっと|あの|じゃあ|それじゃ|ねえ|ちょっと|おい|さあ?|なあ)[、。\s]*)?"
            r"(?:ニューロ|にゅーろ|ねうろ|neuro|ポポ|ぽぽ|poppo)[、。\s]*",
            "", query, flags=re.IGNORECASE).strip(" 　、。を")
        if query in _GENERIC or _looks_omakase(query):
            if re.search(r"有名|人気|定番|ヒット|おすすめ|オススメ|流行|はやり", query):
                return ("play", "J-POP 人気曲")
            return ("play", None)   # おまかせ (AI自身に選曲させる)
        return ("play", query)
    return None


def resolve_track(query: str) -> dict | None:
    """YouTube検索して曲情報を返す (Discord用・ローカル再生用の共通処理)。"""
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # A first YouTube hit can be deleted, region-blocked, or private.
        # Retrieve a small candidate set and let yt-dlp skip unavailable items
        # instead of failing the whole music request on that one result.
        "default_search": "ytsearch5",
        "ignoreerrors": True,
        "source_address": "0.0.0.0",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(query, download=False)
        if data is None:
            return None
        if "entries" in data:
            entries = [e for e in data["entries"] if e and e.get("url")]
            if not entries:
                return None
            data = entries[0]
        webpage = data.get("webpage_url", "") or ""
        is_yt = "youtube.com" in webpage or "youtu.be" in webpage
        return {
            "title": data.get("title", "（不明な曲）"),
            "url": data.get("url"),
            "webpage": webpage,
            "video_id": (data.get("id") or "") if is_yt else "",
        }


# ---------- TTS + 音楽 のミックスソース ----------

def make_mixed_source(discord_mod, tts_source):
    """TTS(QueueAudioSource) と音楽(FFmpeg) を1本に合成する AudioSource を作る。

    bot 側は従来どおり self._source.write()/clear()/playing を使えるよう、
    それらは内側の TTS ソースへ委譲する。
    """

    class MixedAudioSource(discord_mod.AudioSource):
        def __init__(self, tts):
            self._tts = tts
            self._music = None
            self._music_volume = 0.1  # 音楽の初期音量倍率 (10%、0.0〜1.5)。声で調整

        # --- TTS 互換API (bot._speak / _handle_utterance が使用) ---
        def write(self, pcm: bytes) -> None:
            self._tts.write(pcm)

        def clear(self) -> None:
            self._tts.clear()

        @property
        def playing(self) -> bool:
            return self._tts.playing  # TTS(ニューロの声)を再生中か

        # --- 音楽制御 ---
        @property
        def music_active(self) -> bool:
            return self._music is not None

        def set_music(self, src) -> None:
            old = self._music
            self._music = src
            if old is not None:
                try:
                    old.cleanup()
                except Exception:
                    logger.debug("旧音楽ソースの後始末に失敗", exc_info=True)

        def stop_music(self) -> None:
            self.set_music(None)

        @property
        def music_volume(self) -> float:
            return self._music_volume

        def set_music_volume(self, vol: float) -> None:
            self._music_volume = max(0.0, min(1.5, float(vol)))

        # --- discord.py が20ms毎に呼ぶ ---
        def read(self) -> bytes:
            # 例外を絶対に投げない (投げると discord.py のプレイヤーが停止し、
            # 以降 TTS も音楽も出なくなる)。曲切替/停止(別スレッド)との競合も
            # ローカル変数で受けて回避する。
            try:
                mus = self._music  # 参照を固定 (この後 None にされても安全)
                music = mus.read() if mus is not None else b""
                if mus is not None and not music:
                    # この曲は最後まで再生された → 差し替え・後始末
                    if self._music is mus:
                        self._music = None
                    try:
                        mus.cleanup()
                    except Exception:
                        pass
                    music = b""
                tts = self._tts.read()
                if not music:
                    return tts  # 音楽なし → TTSのみ (空ならbで再生停止)
                if len(music) < _FRAME_BYTES:
                    music = music + b"\x00" * (_FRAME_BYTES - len(music))
                elif len(music) > _FRAME_BYTES:
                    music = music[:_FRAME_BYTES]
                vol = self._music_volume
                m = np.frombuffer(music, dtype=np.int16).astype(np.float32)
                if tts:
                    if len(tts) < _FRAME_BYTES:
                        tts = tts + b"\x00" * (_FRAME_BYTES - len(tts))
                    t = np.frombuffer(tts[:_FRAME_BYTES], dtype=np.int16).astype(np.float32)
                    # ニューロ発話中は音楽をダッキング(25%)。さらに音量倍率をかける
                    mixed = np.clip(m * (_MUSIC_DUCK_PCT / 100.0) * vol + t, -32768, 32767)
                    return mixed.astype(np.int16).tobytes()
                if abs(vol - 1.0) < 0.001:
                    return music  # 等倍はそのまま返す (無駄な変換を避ける)
                return np.clip(m * vol, -32768, 32767).astype(np.int16).tobytes()
            except Exception:
                logger.exception("音楽ミックスのread()でエラー")
                # 音楽中は無音でつないでプレイヤーを生かす。それ以外は停止(b"")。
                return _SILENCE if self._music is not None else b""

        def is_opus(self) -> bool:
            return False

        def cleanup(self) -> None:
            self.stop_music()

    return MixedAudioSource(tts_source)


# ---------- 音楽プレイヤー ----------

class _CountingSource:
    """FFmpegソースを包み、再生位置 (元動画の秒数) を追跡する。"""

    def __init__(self, inner, player: "MusicPlayer"):
        self._inner = inner
        self._player = player

    def read(self) -> bytes:
        data = self._inner.read()
        if data:
            # 20msフレームを1回読むごとに、元ソースは 0.02×速度 秒だけ進む
            self._player._pos_s += 0.02 * self._player._speed
        return data

    def cleanup(self) -> None:
        try:
            self._inner.cleanup()
        except Exception:
            pass


def _fmt_pos(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class MusicPlayer:
    """YouTube検索 → キュー → FFmpegでVC再生。曲終わりで自動的に次へ。"""

    def __init__(self, discord_mod, source, get_vc, loop, on_status,
                default_query: str = "作業用BGM", on_video=None,
                allow_without_vc=None):
        self._discord = discord_mod
        self._source = source           # MixedAudioSource
        self._get_vc = get_vc           # callable → 現在のVoiceClient (なければNone)
        self._loop = loop
        self._on_status = on_status     # callable(str)
        self._on_video = on_video       # callable(info, pos_s, speed) GUIの動画カード用
        self._allow_without_vc = allow_without_vc or (lambda: False)
        self._default_query = default_query
        self._queue: list[dict] = []
        self._current: dict | None = None
        self._speed = 1.0    # 再生速度 (atempo)
        self._pos_s = 0.0    # 現在の再生位置 (元動画の秒)
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="music-dl")
        self._watch_task: asyncio.Task | None = None

    @property
    def is_playing(self) -> bool:
        return self._current is not None or self._source.music_active

    @property
    def current_title(self) -> str:
        return self._current.get("title", "") if self._current else ""

    @property
    def current_track(self) -> dict | None:
        return dict(self._current) if self._current else None

    def start_watch(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(self._watch())

    async def play(self, query: str | None) -> dict | None:
        """曲を検索してキューに積む。停止中なら即再生開始。"""
        q = query or self._default_query
        try:
            info = await self._loop.run_in_executor(self._ex, self._resolve, q)
        except Exception as e:
            logger.exception("YouTube検索に失敗")
            self._on_status(f"⚠ 曲が見つからなかった/取得に失敗: {e}")
            return None
        if info is None:
            self._on_status(f"⚠「{q}」が見つからなかった")
            return None
        info["query"] = q
        self._queue.append(info)
        self.start_watch()
        if self._current is None:
            await self._start_next()
        else:
            self._on_status(f"🎵 キューに追加: {info['title']}")
        return info

    def _resolve(self, query: str) -> dict | None:
        return resolve_track(query)

    def _atempo_chain(self) -> str:
        """atempoフィルタは1段0.5〜2.0までなので、範囲外は多段にする。"""
        s = self._speed
        parts = []
        while s > 2.0:
            parts.append("atempo=2.0")
            s /= 2.0
        while s < 0.5:
            parts.append("atempo=0.5")
            s /= 0.5
        parts.append(f"atempo={s:.3f}")
        return ",".join(parts)

    def _spawn(self, vc, pos_s: float) -> bool:
        """現在の曲を pos_s 秒地点・現在の速度でFFmpeg再生する。"""
        item = self._current
        if item is None or not item.get("url"):
            return False
        # -rw_timeout: 15秒データが来なければ落ちる(ネットワーク停止でread()が
        # 永久ブロック→VC音声が丸ごと止まるのを防ぐ。落ちれば曲終了扱いで復帰)
        before = ("-nostdin -rw_timeout 15000000 "
                  "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5")
        if pos_s > 0.5:
            before += f" -ss {pos_s:.1f}"  # 入力前シーク (高速)
        options = "-vn"
        if abs(self._speed - 1.0) > 0.01:
            options += f' -af "{self._atempo_chain()}"'
        try:
            ffmpeg = self._discord.FFmpegPCMAudio(
                item["url"], before_options=before, options=options)
        except Exception as e:
            logger.exception("FFmpegの起動に失敗")
            self._on_status(
                f"⚠ 音楽の再生に失敗: {e} (FFmpegが未インストールかも。導入してPATHを通してね)")
            self._current = None
            return False
        self._pos_s = pos_s
        self._source.set_music(_CountingSource(ffmpeg, self))
        try:
            if vc is not None and not vc.is_playing():
                vc.play(self._source)
        except Exception:
            logger.exception("音楽再生の開始に失敗")
        # GUIへ動画カードを通知 (シーク・速度変更時も位置同期のため再通知)
        if self._on_video is not None:
            try:
                self._on_video(dict(item), pos_s, self._speed)
            except Exception:
                logger.debug("動画カード通知に失敗", exc_info=True)
        return True

    async def _start_next(self) -> None:
        if not self._queue:
            self._current = None
            self._on_status("🎵 再生キューが空になったよ")
            return
        item = self._queue.pop(0)
        vc = self._get_vc()
        if (vc is None and not self._allow_without_vc()) or not item.get("url"):
            self._current = None
            return
        self._current = item
        if self._spawn(vc, 0.0):
            note = f" ({self._speed:g}倍速)" if abs(self._speed - 1.0) > 0.01 else ""
            self._on_status(f"🎵 再生中: {item['title']}{note}")

    async def seek(self, delta_s: float) -> None:
        """現在の曲を相対シーク (負=巻き戻し)。"""
        if self._current is None:
            self._on_status("🎵 今は何も流れてないよ")
            return
        new_pos = max(0.0, self._pos_s + float(delta_s))
        if self._spawn(self._get_vc(), new_pos):
            arrow = "⏩" if delta_s >= 0 else "⏪"
            self._on_status(
                f"{arrow} {abs(int(delta_s))}秒{'進めた' if delta_s >= 0 else '戻した'}よ "
                f"(現在 {_fmt_pos(new_pos)})"
            )

    async def seek_abs(self, pos_s: float) -> None:
        """絶対位置へシーク (0=最初から)。"""
        if self._current is None:
            self._on_status("🎵 今は何も流れてないよ")
            return
        if self._spawn(self._get_vc(), max(0.0, float(pos_s))):
            self._on_status("🎵 最初から再生するね" if pos_s <= 0
                            else f"🎵 {_fmt_pos(pos_s)} から再生するね")

    async def set_speed(self, speed: float) -> None:
        """再生速度を変更する (0.5〜3.0倍)。現在位置を保って切り替え。"""
        if self._current is None:
            self._on_status("🎵 今は何も流れてないよ")
            return
        self._speed = max(0.5, min(3.0, float(speed)))
        if self._spawn(self._get_vc(), self._pos_s):
            if abs(self._speed - 1.0) <= 0.01:
                self._on_status("▶ 通常速度に戻したよ")
            else:
                self._on_status(f"▶ {self._speed:g}倍速にしたよ (現在 {_fmt_pos(self._pos_s)})")

    def set_volume(self, vol: float) -> None:
        """音楽の音量を絶対値で設定 (0.0〜1.5)。FFmpeg再生成は不要。"""
        v = max(0.0, min(1.5, float(vol)))
        self._source.set_music_volume(v)
        self._on_status(f"🔊 音量を {int(round(v * 100))}% にしたよ")

    def volume_step(self, delta: float) -> None:
        """音楽の音量を相対的に上げ下げする。"""
        cur = getattr(self._source, "music_volume", 1.0)
        v = max(0.0, min(1.5, cur + float(delta)))
        self._source.set_music_volume(v)
        if v <= 0.0:
            self._on_status("🔇 音楽をミュートしたよ (上げてで戻せる)")
        else:
            arrow = "🔊" if delta >= 0 else "🔉"
            self._on_status(f"{arrow} 音量 {int(round(v * 100))}%")

    async def skip(self) -> None:
        if self._current is None and not self._queue:
            self._on_status("🎵 今は何も流れてないよ")
            return
        self._source.stop_music()
        self._current = None
        await self._start_next()

    def stop(self) -> None:
        self._queue.clear()
        self._current = None
        self._speed = 1.0  # 次の曲は通常速度から
        self._source.stop_music()
        self._on_status("🎵 音楽を止めたよ")

    def clear_queue(self) -> int:
        """予約済みの曲だけを削除し、現在の曲は再生し続ける。"""
        count = len(self._queue)
        self._queue.clear()
        self._on_status("🎵 キューを空にしたよ" if count else "🎵 キューはすでに空だよ")
        return count

    async def _watch(self) -> None:
        """曲が終わったのを検知して次の曲へ。VC再生が落ちていたら復帰させる。"""
        try:
            while True:
                await asyncio.sleep(0.5)
                vc = self._get_vc()
                if self._current is not None and not self._source.music_active:
                    # 現在の曲が最後まで再生された
                    self._current = None
                    if self._queue:
                        await self._start_next()
                        continue
                    self._on_status("🎵 再生終了")
                # セーフティ: 音楽が残っているのにVC再生が止まっていたら再開する
                if (self._source.music_active and vc is not None
                        and not vc.is_playing()):
                    try:
                        vc.play(self._source)
                    except Exception:
                        logger.debug("音楽再生の復帰に失敗", exc_info=True)
        except asyncio.CancelledError:
            pass

    def shutdown(self) -> None:
        if self._watch_task is not None:
            self._watch_task.cancel()
            self._watch_task = None
        self._queue.clear()
        self._current = None
        try:
            self._source.stop_music()
        except Exception:
            pass
