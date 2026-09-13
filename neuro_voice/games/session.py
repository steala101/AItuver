"""Shared local/Discord game-profile session semantics."""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from neuro_voice.activity import ActivityOutcome
from neuro_voice.games.ktane import KtaneExpert
from neuro_voice.games.ktane.tables import load_tables
from neuro_voice.games.profiles import GameProfile, GameProfileRegistry


_KTANE_ID = "keep_talking_and_nobody_explodes"
_START = re.compile(
    r"(?:keep\s*talking|ktane|完全爆弾解除マニュアル|爆弾解除|爆弾処理班)"
    r".*(?:やろう|始め|開始|遊ぼう)|"
    r"(?:爆弾解除|爆弾処理班)(?:しよう|やる)$|"
    r"(?:この)?(?:ゲーム|セッション)(?:を)?(?:始めよう|開始しよう|やろう)$",
    re.IGNORECASE,
)
_STOP = re.compile(
    r"(?:爆弾解除|爆弾処理班|keep\s*talking|ktane).*(?:やめ|終了|終わ)|"
    r"(?:ゲーム|セッション)(?:を)?(?:やめよう|終了)",
    re.IGNORECASE,
)
_SHORT_STOP = re.compile(r"^(?:もう)?(?:やめよう|終わりにしよう|終了しよう)[。.!！]?$")
#: 爆弾がどうなったか。**言われた時だけ。推測しない。**
#:
#: 画面は見ていないので、結果はユーザーが言うまで分からない。
#: 「終わった」は解除できたとは限らない——時間切れかもしれないし、
#: 単に飽きただけかもしれない。分からないものを分かったことにしない。
_DEFUSED = re.compile(r"解除(?:でき|成功|完了)|(?:爆弾|ボム).{0,4}(?:止め|止まっ)|クリア")
_EXPLODED = re.compile(r"爆発(?:した|しちゃ|され)|(?:爆弾|ボム).{0,4}爆発|ドカン|失敗した")
_STATUS = re.compile(r"(?:爆弾解除|爆弾処理班|ktane).*(?:状態|状況)|(?:今|現在)の(?:モジュール|爆弾)は")

#: 「切り替えて」と読める言い方。**単独では何も起こさない**。
#:
#: 「静かにして」も「にして」で当たってしまうので、これに加えて
#: プロファイルの別名が同じ文の中にあることを必須にしている。両方そろって
#: はじめて切り替えを検討する。片方だけなら普通の会話として通す。
_SWITCH = re.compile(
    r"(?:切り替え|きりかえ|切替|変えて|変更|戻して|もどして|にして|へして|起動)",
)
#: 別名が見つからなかった時に「どのゲーム？」と聞き返してよい文脈かどうか。
_GAME_WORD = re.compile(r"ゲーム|モード|プロファイル|profile", re.IGNORECASE)
#: 「今どのモード？」
_WHICH = re.compile(
    r"(?:今|いま|現在).{0,6}(?:どの|なんの|何の).{0,6}(?:ゲーム|モード|プロファイル)"
    r"|(?:ゲーム|プロファイル)?モードは(?:今|いま)?(?:何|なに|どれ)"
    r"|今の(?:ゲーム|モード|プロファイル)",
)


@dataclass(slots=True)
class GameSessionSnapshot:
    profile_id: str
    active: bool
    session_id: str
    started_at: float
    turn_count: int
    source: str


class GameProfileSessionManager:
    """Owns only canonical session facts; conversation wording stays in the LLM."""

    def __init__(self, cfg, state_path: str | Path):
        self._cfg = cfg
        root = str(cfg.get("game_profiles.root", "") or "")
        self.registry = GameProfileRegistry(root or None)
        self._state_path = Path(state_path)
        self._active = False
        self._active_profile_id = ""
        self._session_id = ""
        self._started_at = 0.0
        self._turn_count = 0
        self._source = ""
        #: 爆弾1つぶんの記憶。セッションを開始するたびに作り直す。
        self._expert: KtaneExpert | None = None
        #: 出来事の受け取り手。**ここは世界状態を知らない。**
        #:
        #: セッション管理が `InitiativeRuntime` を直接呼ぶと、ゲームの意味論と
        #: 認知層が絡んで片方だけ直せなくなる。出来事を渡すだけにしておく。
        self._observers: list = []
        #: 同じ状態を二度配らないための控え。
        self._last_module = ""
        self._last_strikes = -1
        self._last_outcome = ""
        self._load()

    # ------------------------------------------------------------------
    # 出来事の通知（KTANE）
    #
    # **画面認識は無い。** ここが出す出来事はすべて会話から読み取ったもので、
    # 根拠はユーザーの発話。画面から爆弾の状態を読む経路は作らない——
    # 当たらない認識を根拠に「切って」と言うと、爆発する。
    # ------------------------------------------------------------------

    def add_observer(self, callback) -> None:
        """出来事を受け取る。**例外は握りつぶす**（第17条）。"""
        if callable(callback) and callback not in self._observers:
            self._observers.append(callback)

    def _notify(self, event: str, **payload) -> None:
        for observer in self._observers:
            try:
                observer(event=event, session_id=self._session_id, **payload)
            except Exception:  # noqa: BLE001 — 観測の失敗でゲームを止めない
                pass

    def _notify_expert_state(self) -> None:
        """モジュールとミス回数が変わっていたら配る。**変化した時だけ。**"""
        expert = self._expert
        if expert is None:
            return
        module = str(expert.current_module or "")
        if module and module != self._last_module:
            self._last_module = module
            self._notify("module_detected", module=module)
        strikes = int(getattr(expert.edge, "strikes", 0) or 0)
        if strikes != self._last_strikes:
            self._last_strikes = strikes
            self._notify("strike_recorded", strikes=strikes)

    def _session_outcome(self, value: str) -> str:
        """終わり方。**言われていないなら `unknown`。**"""
        if _EXPLODED.search(value):
            return "exploded"
        if _DEFUSED.search(value):
            return "defused"
        return "unknown"

    @property
    def selected(self) -> GameProfile | None:
        return self.registry.get(self._cfg.get("video.game_profile", ""))

    @property
    def active(self) -> bool:
        selected = self.selected
        return bool(
            self._active and selected is not None and self._active_profile_id == selected.id
        )

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._active = bool(data.get("active", False))
        self._active_profile_id = str(data.get("profile_id", ""))
        self._session_id = str(data.get("session_id", ""))
        self._started_at = float(data.get("started_at", 0.0))
        self._turn_count = int(data.get("turn_count", 0))
        self._source = str(data.get("source", ""))

    def save(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "profile_id": self._active_profile_id,
            "active": self._active,
            "session_id": self._session_id,
            "started_at": self._started_at,
            "turn_count": self._turn_count,
            "source": self._source,
        }
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)

    def snapshot(self) -> dict:
        profile = self.selected
        return {
            "profile_id": profile.id if profile else "",
            "profile_label": profile.label if profile else "",
            "active": self.active,
            "session_id": self._session_id,
            "started_at": self._started_at,
            "turn_count": self._turn_count,
            "source": self._source,
            "assistant_role": profile.assistant_role if profile else "",
            "vision_policy": profile.vision_policy if profile else "none",
        }

    # ------------------------------------------------------------------
    # 会話からの切り替え
    # ------------------------------------------------------------------

    def _named_profile(self, text: str) -> GameProfile | None:
        """発話の中に名前が出ているプロファイル。長い別名を優先する。"""
        value = str(text or "").casefold()
        for alias, profile_id in self.registry.alias_items():
            if alias and alias in value:
                return self.registry.get(profile_id)
        return None

    def switch_to(self, profile: GameProfile) -> bool:
        """選択中のプロファイルを変える。変わったら True。

        `selected` は毎回 config を読み直すので、ここで書き換えれば
        再起動なしで効く。`persist` は config.yaml へも書き戻すため、
        次回の起動でも同じモードで立ち上がる。

        進行中のセッションは終了させる。別のゲームへ移ったのに爆弾解除の
        セッションが生きていると、以後の発話が持ち主のいない状態へ入る
        （第5条）。
        """
        current = self.selected
        if current is not None and current.id == profile.id:
            return False
        if self._active:
            self._active = False
            self.save()
        self._cfg.persist("video.game_profile", profile.id)
        return True

    def _available(self) -> str:
        return "、".join(self.registry.labels()) or "なし"

    def _switch_outcome(self, value: str, source: str) -> ActivityOutcome | None:
        """切り替え依頼として扱えるなら結果を返す。違うなら None。"""
        if not _SWITCH.search(value):
            return None
        target = self._named_profile(value)
        if target is None:
            # 「ゲーム」「モード」と言っているのに名前が分からない時だけ聞き返す。
            # 「静かにして」で在庫一覧を読み上げ始めないための線引き。
            if _GAME_WORD.search(value):
                return ActivityOutcome(
                    handled=True,
                    reply=f"そのゲームのプロファイルはまだ無いよ。今使えるのは{self._available()}。",
                    reason="game_profile_unknown",
                )
            return None
        changed = self.switch_to(target)
        # 「爆弾解除モードにして」と言われた時点で、その人は始めるつもりでいる。
        # 開始語まで揃っているならセッションも開ける。
        if target.id == _KTANE_ID and _START.search(value):
            return self._start(target, source, switched=changed)
        return ActivityOutcome(
            handled=True,
            reply=(
                f"了解、{target.label}に切り替えたよ。" if changed
                else f"今も{target.label}のままだよ。"
            ),
            reason="game_profile_switched" if changed else "game_profile_unchanged",
        )

    def _start(self, profile: GameProfile, source: str, *, switched: bool) -> ActivityOutcome:
        # 前の爆弾の記憶を持ち越さない。シリアルも電池も別物になる。
        self._expert = self._new_expert()
        self._active = True
        self._active_profile_id = profile.id
        self._session_id = uuid.uuid4().hex
        self._started_at = time.time()
        self._turn_count = 0
        self._source = source
        self._last_module = ""
        self._last_strikes = -1
        self._last_outcome = ""
        self.save()
        # **開始はユーザーが明示した時だけ。** 確信を高く置けるのはそのため。
        self._notify("bomb_started", confidence=.95)
        opening = "了解、爆弾解除モードに切り替えたよ。" if switched else "了解、"
        return ActivityOutcome(
            handled=True,
            reply=(
                f"{opening}私はマニュアル担当ね。爆弾の画面は見ないから、"
                "まず最初のモジュール名と見えている情報を短く教えて。"
            ),
            reason=(
                "game_profile_switched_and_started" if switched
                else "game_profile_session_started"
            ),
            state_version=0,
        )

    def handle_final_input(
        self, text: str, *, actor_id: str, source: str, utterance_id: str | None = None,
    ) -> ActivityOutcome:
        del actor_id, utterance_id
        value = str(text or "").strip()
        if not value:
            return ActivityOutcome(handled=False)

        # 切り替えと状態確認は、どのプロファイルが選ばれていても受ける。
        # 以前はここでKTANE以外を弾いていたので、マイクラのまま
        # 「爆弾解除を始めよう」と言っても何も起きなかった。
        switch = self._switch_outcome(value, source)
        if switch is not None:
            return switch
        if _WHICH.search(value):
            current = self.selected
            if current is None:
                return ActivityOutcome(
                    handled=True,
                    reply=f"今はゲームモードを選んでないよ。使えるのは{self._available()}。",
                    reason="game_profile_status",
                )
            return ActivityOutcome(
                handled=True,
                reply=(
                    f"今は{current.label}。セッションは"
                    f"{'進行中' if self.active else 'まだ始めてない'}よ。"
                ),
                reason="game_profile_status",
            )
        # 名前を言わずに「爆弾解除を始めよう」と言われた場合も、開始語の中に
        # ゲーム名が入っていれば切り替えて始める。
        if _START.search(value):
            named = self._named_profile(value)
            if named is not None and named.id == _KTANE_ID:
                return self._start(named, source, switched=self.switch_to(named))

        profile = self.selected
        if profile is None or profile.id != _KTANE_ID:
            return ActivityOutcome(handled=False)
        if (_STOP.search(value) or _SHORT_STOP.search(value)) and self.active:
            self._active = False
            self._turn_count += 1
            self.save()
            self._notify("bomb_ended", outcome=self._session_outcome(value))
            return ActivityOutcome(
                handled=True,
                reply="了解、爆弾解除セッションを終了するね。",
                reason="game_profile_session_stopped",
                state_version=self._turn_count,
            )
        if _START.search(value):
            return self._start(profile, source, switched=False)
        if self._active:
            self._turn_count += 1
            self.save()
            defusal = self._defusal_outcome(value)
            # モジュールとミス回数を配る。**解けたかどうかとは別**——
            # 答えを返さないターンでも、状態は変わっている。
            self._notify_expert_state()
            # **爆弾が終わったことと、セッションが終わったことは別。**
            # 爆発しても続けて次の爆弾をやることはある。
            outcome = self._session_outcome(value)
            if outcome != "unknown" and outcome != self._last_outcome:
                self._last_outcome = outcome
                self._notify("bomb_ended", outcome=outcome)
            if defusal is not None:
                return defusal
            if _STATUS.search(value):
                return ActivityOutcome(
                    handled=True,
                    reply="セッションは継続中。今扱うモジュール名と、そこに見える文字・色・本数を教えて。",
                    reason="game_profile_session_status",
                    state_version=self._turn_count,
                )
        return ActivityOutcome(handled=False, state_version=self._turn_count)

    # ------------------------------------------------------------------
    # 爆弾解除の判断
    # ------------------------------------------------------------------

    @property
    def expert(self) -> KtaneExpert:
        if self._expert is None:
            self._expert = self._new_expert()
        return self._expert

    def _new_expert(self) -> KtaneExpert:
        """表はプロファイルのフォルダから読む。無ければ空のまま動く。"""
        profile = self.registry.get(_KTANE_ID)
        return KtaneExpert(tables=load_tables(profile.folder if profile else None))

    def _defusal_outcome(self, value: str) -> ActivityOutcome | None:
        """規則で答えが出るなら、LLMを通さずそのまま返す。

        12Bのモデルに「赤が2本以上でシリアル末尾が奇数なら最後の赤」を
        解かせると間違える。爆弾では間違いが爆発になる（第2条）。
        確定しない時は None を返し、普通の会話として処理させる。
        """
        expert = self.expert
        learned = expert.observe(value)
        solution = expert.solve(value)
        if solution is None:
            if not learned:
                return None
            return ActivityOutcome(
                handled=True,
                reply=f"{'と'.join(learned)}を覚えた。次はどのモジュール？",
                reason="ktane_edgework_recorded",
                state_version=self._turn_count,
            )
        if solution.unknown:
            return ActivityOutcome(
                handled=True,
                reply=(
                    f"ごめん、{solution.because}。"
                    "適当な指示はできないから、そのモジュールは自分で見て。"
                ),
                reason="ktane_rule_missing",
                state_version=self._turn_count,
            )
        if solution.solved:
            # **答えをここで返さない。** 規則はプロンプトへ渡してあるので、
            # ポッポが自分で読んで考える。その発話を `verify_reply()` で
            # この答えと突き合わせ、食い違った時だけ差し替える（第2条）。
            #
            # 以前はここで固定文を返しており、ポッポのLLMを一度も通って
            # いなかった。声だけポッポで、言葉は表引きの結果だった。
            expert.pending_check = solution
            return None
        if solution.needs:
            reply = solution.needs
            if solution.because:
                reply = f"{solution.because}。{reply}"
            return ActivityOutcome(
                handled=True,
                reply=f"{reply}を教えて。",
                reason="ktane_needs_more",
                state_version=self._turn_count,
            )
        return None

    def grounded_context(self) -> str | None:
        profile = self.selected
        if profile is None:
            return None
        if profile.interaction_mode == "manual_expert" and not self.active:
            # 以前はここで None を返していた。そのため「爆弾解除モードにして」
            # のあと「マニュアルは何版？検証コードは？」と聞かれても、
            # ポッポの手元には**何の情報も無かった**。しかもKTANE中は
            # ウェブ検索も止まるので、答えようがない。
            #
            # 「始めよう」と言うまで版すら答えられない、というのは
            # 利用者から見えない前提条件だった。開始前は短い参照だけ渡す。
            reference = profile.manual_reference
            return (
                f"【選択中のゲームプロファイル】{profile.label}（セッションは未開始）\n"
                f"ポッポの役割: {profile.assistant_role}\n"
                + (reference + "\n" if reference else "")
                + "まだ始まっていないので手順の指示はしない。"
                "ただし版・検証コードのように今分かることは、そのまま普通に答える。"
            )
        if (
            profile.interaction_mode == "visual_companion"
            and not bool(self._cfg.get("video.enabled", False))
        ):
            return None
        active_note = "進行中" if self.active else "未開始"
        text = (
            f"【選択中のゲームプロファイル】{profile.label}\n"
            f"ポッポの役割: {profile.assistant_role}\n"
            f"セッション: {active_note}\n"
            f"映像方針: {profile.vision_policy}\n"
            f"{profile.conversation_instruction}"
        )
        if profile.id == _KTANE_ID:
            # 版と検証コードは profile.json が正本。ここに書き写すと、
            # profile.json を直しても古い値を言い続ける（実際そうなっていた）。
            reference = profile.manual_reference
            rules = self.expert.rule_prompt()
            text += (
                ("\n" + reference if reference else "")
                + "\nプレイ中は一般Web検索を行わない。"
                "\n" + self.expert.context()
                + ("\n" + rules if rules else "")
                + "\n上の規則を自分で読んで、当てはまる行を選んで指示する。"
                "上から順に見て、最初に当てはまった行で決まる。"
                "足りない情報があれば、断定せず一つだけ尋ねる。"
                # 実機で、表が空のキーパッドに「一番上のプサイを押して」と
                # 言って爆発した。**上に規則が無いモジュールは答えを持って
                # いない**ことを、その場で言う。
                "\n**上に規則が載っていないモジュールについては、"
                "切る線・押すボタン・離すタイミングを言わない。**"
                "推測で操作を指示すると爆発する。手元に無いと正直に言う。"
            )
        return text

    def check_pending(self) -> bool:
        """検算すべき答えを抱えているか。"""
        return bool(
            self.active and self._expert is not None
            and self._expert.pending_check is not None
        )

    def verify_reply(self, reply: str) -> tuple[str, str]:
        """ポッポの発話を検算する。(判定, 差し替える文) を返す。

        差し替える文が空なら、そのまま話してよい。セッション中は全文を
        保持してから発話する作りなので、ここで差し替えれば**間違った指示は
        耳に届かない**。
        """
        if not self.active or self._expert is None:
            return "unclear", ""
        return self._expert.verify(reply)
