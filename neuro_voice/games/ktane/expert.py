"""聞いた話をためて、答えが出せる時だけ答える。

一回の発話で全部そろうことはまずない。「配線が4本」だけ言われて色は次の発話、
シリアルはさらにその後、ということが普通に起きる。だから状態を持つ。

**答えが確定した時はLLMを通さずそのまま返す。** 12Bのモデルに
「赤が2本以上でシリアル末尾が奇数なら最後の赤」を解かせると間違える。
確定しない時だけ会話へ渡し、足りないものを一つ尋ねる。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from neuro_voice.games.ktane import modules as rules
from neuro_voice.games.ktane.edgework import Edgework
from neuro_voice.games.ktane.modules import MemoryPress, Solution
from neuro_voice.games.ktane.parse import (
    parse_button_label, parse_colors, parse_coordinates, parse_count, parse_module,
    parse_symbols, parse_words, update_edgework,
)
from neuro_voice.games.ktane import verify
from neuro_voice.games.ktane.rulebook import rule_text
from neuro_voice.games.ktane.tables import KtaneTables

#: 人へ言うときのモジュール名。
MODULE_NAMES = {
    "wires": "配線",
    "button": "ボタン",
    "keypad": "キーパッド",
    "simon": "サイモン",
    "whos_on_first": "心理戦",
    "memory": "記憶",
    "morse": "モールス信号",
    "complicated_wires": "複雑な配線",
    "wire_sequences": "順番に配線",
    "maze": "迷路",
    "password": "パスワード",
}
#: 解法をコードに持っているモジュール。
#:
#: キーパッド・心理戦・迷路は表が大きく、記憶で書き起こすと静かに誤る。
#: 解法はここにあるが、表は `manual/tables/*.json` から読む。未取り込みなら
#: 「まだ取り込んでいない」と言って答えない（`tables.py` を参照）。
IMPLEMENTED = frozenset({
    "wires", "button", "simon", "memory", "morse",
    "complicated_wires", "wire_sequences", "password",
    "keypad", "whos_on_first", "maze",
})
#: 表が要るもの。取り込み済みかどうかで答えられるかが変わる。
NEEDS_TABLE = frozenset({"keypad", "whos_on_first", "maze"})


@dataclass(slots=True)
class KtaneExpert:
    """1つの爆弾ぶんの記憶。セッションが終わったら捨てる。"""

    edge: Edgework = field(default_factory=Edgework)
    current_module: str = ""
    #: 記憶モジュール用。押した位置と数字を段ごとに覚える。
    memory_history: list[MemoryPress] = field(default_factory=list)
    #: 「左から2番目を押して」と言った直後は、位置は分かっていて数字だけ不明。
    #: 逆に「『4』を押して」なら数字は分かっていて位置が不明。分かっている方を
    #: ここへ置き、次の発話で足りない方だけを受け取る。
    pending_memory_position: int | None = None
    pending_memory_label: int | None = None
    #: モールスで読み取れた文字を継ぎ足していく。
    morse_letters: str = ""
    #: 外から読んだ表。空でも動く（そのモジュールに答えないだけ）。
    tables: KtaneTables = field(default_factory=KtaneTables)
    #: **いま扱っているモジュールについて聞けたこと。**
    #:
    #: 爆弾の情報（シリアル・電池）は溜めるのに、色や文字は毎回その発話
    #: だけから読み直していた。だから「色は白」と言った次のターンで
    #: また色を聞き、実機で同じ質問が繰り返された。
    #: モジュールが変わったら捨てる。
    slots: dict[str, Any] = field(default_factory=dict)
    #: 記号の言われ方を覚えていく。毎回同じ呼び名を要求しないため。
    vocabulary: Any = None
    #: 直前に「どれのこと？」と聞いた呼び名。答えが来たら紐づける。
    pending_symbol_alias: str = ""
    #: 心理戦・迷路は複数の発話をまたいで組み立てる。
    who_buttons: list[str] = field(default_factory=list)
    maze_circles: list[tuple[int, int]] = field(default_factory=list)
    maze_start: tuple[int, int] | None = None
    maze_goal: tuple[int, int] | None = None
    #: 直前に返した質問。同じ一文を2回続けて返さないため。
    _last_needs: str = ""
    #: このターン、コードが出した答え。ポッポの発話と突き合わせるまで持つ。
    pending_check: Solution | None = None
    #: 一致・不一致・判定不能の回数。分担が機能しているかを見るため。
    agreement_counts: dict[str, int] = field(
        default_factory=lambda: {"agree": 0, "disagree": 0, "unclear": 0},
    )

    def reset(self) -> None:
        self.edge = Edgework()
        self.current_module = ""
        self.memory_history.clear()
        self.pending_memory_position = None
        self.pending_memory_label = None
        self.morse_letters = ""
        self.who_buttons.clear()
        self.maze_circles.clear()
        self.maze_start = None
        self.maze_goal = None
        self._last_needs = ""
        self.pending_check = None

    # ------------------------------------------------------------------

    def observe(self, text: str) -> list[str]:
        """発話から爆弾情報を拾う。答えは出さない。"""
        return update_edgework(self.edge, text)

    def note_module(self, text: str) -> str:
        module = parse_module(text)
        if module and module != self.current_module:
            # 別のモジュールへ移ったら、前のモジュールで聞いたことは捨てる。
            # 持ち越すと、赤い配線の話がボタンの色として残る。
            self.slots.clear()
            self.current_module = module
        return self.current_module

    def solve(self, text: str) -> Solution | None:
        """今の発話で答えが出せるなら出す。出せなければ None。

        `None` は「このモジュールは今は扱えない」ではなく
        「この発話だけでは何も確定しない」の意味。会話側へ渡す。
        """
        module = self.note_module(text)
        if not module:
            return None
        if module not in IMPLEMENTED:
            return rules.unknown_module(MODULE_NAMES.get(module, module))
        name = MODULE_NAMES.get(module, module)
        # キーパッドは表が無くても**口で教えられる**ので、ここで断らない。
        if module in NEEDS_TABLE and module != "keypad" and name in self.tables.missing():
            # 名前が出た時点で断る。座標や記号を聞き出してから
            # 「やっぱり分からない」と言うのは、相手の時間を無駄にする。
            return rules.Solution(
                unknown=True,
                because=f"{name}の表をまだ取り込んでいない（docs/KTANE_TABLES.md の手順で入れられる）",
            )
        handler = getattr(self, f"_solve_{module}")
        solution = handler(text)
        return self._without_repeating_itself(solution)

    def _without_repeating_itself(self, solution: Solution | None) -> Solution | None:
        """同じ質問を2回続けて返さない。

        `current_module` は言い直すまで残るので、一度「ボタン」と言われると
        以後の発話はすべてボタンの判定へ入る。そこが質問を返し続けると、
        何を言っても同じ一文が返る壊れ方になる（実際そうなった）。

        2度目は黙って会話側へ渡す。ポッポが普通に受け答えできるし、
        必要ならもう一度こちらから聞き直せる。
        """
        if solution is None or not solution.needs:
            self._last_needs = ""
            return solution
        if solution.needs == self._last_needs:
            self._last_needs = ""
            return None
        self._last_needs = solution.needs
        return solution

    # -- 各モジュール ---------------------------------------------------

    def _solve_wires(self, text: str) -> Solution | None:
        colors = parse_colors(text)
        count = parse_count(text) or self.slots.get("count")
        if count:
            self.slots["count"] = count
        if colors:
            # 「赤と青」「あと赤と黒」のように分けて言われても組み立てる。
            # 1発話に全部が入る前提だと、途中で止まったまま同じ質問を繰り返す。
            known = list(self.slots.get("colors", []))
            self.slots["colors"] = (known + colors) if len(colors) < 3 else colors
        colors = list(self.slots.get("colors", []))
        if count and len(colors) > int(count):
            colors = colors[-int(count):]
        if len(colors) < 3 or (count and len(colors) < int(count)):
            if not count and not colors:
                return None
            missing = (int(count) - len(colors)) if count else 0
            return Solution(
                needs=f"残り{missing}本の色" if missing > 0 else "配線の色を上から順に",
            )
        return rules.solve_wires(colors, self.edge)

    def _solve_button(self, text: str) -> Solution | None:
        import re

        strip = re.search(r"(?:帯|ライン|バー|光|ひかり)[^。]{0,10}?(赤|青|白|黄|緑)", text)
        if strip:
            return rules.release_on({
                "赤": "red", "青": "blue", "白": "white", "黄": "yellow", "緑": "green",
            }[strip.group(1)])
        colors = parse_colors(text)
        # 文字を尋ねた直後なら、発話まるごとが答え。「長押し」の一言でも採る。
        # 尋ねていなければ「押す」は普通の動詞として扱う（`parse.py` を参照）。
        label = parse_button_label(text, expected=bool(self.slots.get("asked_label")))
        # **聞けたことは溜める。** 色と文字が別のターンで来ても組み立てる。
        if colors:
            self.slots["color"] = colors[0]
        if label:
            self.slots["label"] = label
            self.slots.pop("asked_label", None)
        color = self.slots.get("color", "")
        label = self.slots.get("label", "")
        if not color and not label:
            # 手がかりが1つも無い発話。ここで質問を返すと、シリアルを
            # 言っただけのターンにも「色と文字を教えて」と返し続けることになる
            # （`current_module` は言い直すまで残るため）。会話側へ渡す。
            return None
        if not color:
            return Solution(needs="ボタンの色")
        if not label:
            # **日本語で聞く。** 英語で列挙していたせいで、利用者が
            # 「push」「hold」と英語で言わないと通らないと学習してしまった。
            self.slots["asked_label"] = True
            return Solution(
                needs="ボタンに書いてある文字（中止 / 爆破 / 長押し / 押す のどれか。"
                      "英語なら Abort / Detonate / Hold / Press）",
            )
        return rules.solve_button(color, label, self.edge)

    def _solve_simon(self, text: str) -> Solution | None:
        colors = [c for c in parse_colors(text) if c in {"red", "blue", "green", "yellow"}]
        if not colors:
            return None
        return rules.solve_simon(colors, self.edge)

    def _solve_memory(self, text: str) -> Solution | None:
        import re

        # 直前に出した指示の続き。押した結果を受け取るのが最優先。
        completed = self._complete_memory_press(text)
        if completed is not None:
            return completed

        stage = None
        display = None
        stage_match = re.search(r"(\d)\s*(?:段目|ステージ|回目)", text)
        if stage_match:
            stage = int(stage_match.group(1))
        display_match = re.search(r"(?:画面|表示|ディスプレイ)[^0-9]{0,6}(\d)", text)
        if display_match:
            display = int(display_match.group(1))
        if stage is None:
            stage = len(self.memory_history) + 1
        if display is None:
            return None
        solution = rules.solve_memory(stage, display, self.memory_history)
        self._remember_instruction(solution)
        return solution

    def _remember_instruction(self, solution: Solution) -> None:
        """今出した指示から、分かっている方を控える。

        4段目以降は「1段目でどこを押したか」「2段目で何が書いてあったか」の
        両方が要る。指示した側は片方を知っているので、聞き返すのは片方だけで済む。
        """
        import re

        if not solution.solved:
            return
        position = re.search(r"左から(\d)番目", solution.answer)
        label = re.search(r"「(\d)」", solution.answer)
        self.pending_memory_position = int(position.group(1)) if position else None
        self.pending_memory_label = int(label.group(1)) if label else None
        if position:
            solution.follow_up = "そこに何て書いてあった？"
        elif label:
            solution.follow_up = "それは左から何番目だった？"

    def _complete_memory_press(self, text: str) -> Solution | None:
        """不足していた方を受け取って履歴へ入れる。"""
        import re

        if self.pending_memory_position is None and self.pending_memory_label is None:
            return None
        if re.search(r"(?:画面|表示|ディスプレイ)", text):
            return None                      # 次の段の申告なので、こちらではない
        number = re.search(r"(\d)", text)
        if number is None:
            return None
        value = int(number.group(1))
        if not 1 <= value <= 4:
            return None
        if self.pending_memory_position is not None:
            press = MemoryPress(position=self.pending_memory_position, label=value)
        else:
            press = MemoryPress(position=value, label=self.pending_memory_label)
        self.record_memory_press(press.position, press.label)
        stage = len(self.memory_history)
        return Solution(
            f"{stage}段目は左から{press.position}番目の「{press.label}」だったね。覚えた",
            "",
            follow_up="次の段の画面の数字を教えて",
        )

    def record_memory_press(self, position: int, label: int) -> None:
        """押した結果を覚える。4段目以降はこれが無いと解けない。"""
        self.memory_history.append(MemoryPress(position=position, label=label))
        self.pending_memory_position = None
        self.pending_memory_label = None

    def _solve_morse(self, text: str) -> Solution | None:
        import re

        letters = "".join(re.findall(r"[a-zA-Z]", text))
        if not letters:
            return None
        self.morse_letters = letters if len(letters) > len(self.morse_letters) else self.morse_letters
        return rules.solve_morse(self.morse_letters)

    def _solve_complicated_wires(self, text: str) -> Solution | None:
        import re

        colors = set(parse_colors(text))
        star = bool(re.search(r"星|ほし|スター|star|☆|★", text))
        led = bool(re.search(r"LED|ランプ|点灯|光って", text, re.IGNORECASE))
        if not colors and not star and not led:
            return None
        return rules.solve_complicated_wire(
            red="red" in colors, blue="blue" in colors, star=star, led=led, edge=self.edge,
        )

    def _solve_wire_sequences(self, text: str) -> Solution | None:
        import re

        colors = [c for c in parse_colors(text) if c in {"red", "blue", "black"}]
        occurrence = None
        occurrence_match = re.search(r"(\d)\s*本目", text)
        if occurrence_match:
            occurrence = int(occurrence_match.group(1))
        target = ""
        target_match = re.search(r"\b([ABCabc])\b|([ABC])(?:に|へ)", text)
        if target_match:
            target = (target_match.group(1) or target_match.group(2) or "").upper()
        if not colors:
            return None
        if occurrence is None:
            return Solution(needs=f"その色の線がこれで何本目か")
        if not target:
            return Solution(needs="繋がっている先（A・B・Cのどれか）")
        return rules.solve_wire_sequence(colors[0], occurrence, target)

    def _solve_keypad(self, text: str) -> Solution | None:
        import re

        from neuro_voice.games.ktane.symbols import (
            SymbolVocabulary, known_symbols, resolve_symbols,
        )

        if self.vocabulary is None:
            self.vocabulary = SymbolVocabulary()

        columns = self.tables.keypad_columns
        # 表を口で教える。JSONを手で書かなくても、一度読み上げれば覚える。
        teaching = re.search(r"(\d)\s*列目", text)
        if teaching:
            # 「1列目は …」の前置きを落としてから記号を取る。残すと
            # 「1列目は 丸に線」がまるごと1つの呼び名になる。
            body = text[teaching.end():].lstrip("はがのを、: 　")
            taught = parse_symbols("記号は " + body, limit=9) if body else []
            if taught:
                return self._teach_keypad_column(int(teaching.group(1)), taught)
        if not columns:
            return rules.solve_keypad([], columns)

        spoken = parse_symbols(text)
        if not spoken:
            return None
        # **聞けた記号は溜める。** 4つを1発話で言い切れるとは限らない。
        names = known_symbols(columns)
        # 直前に聞き返した呼び名へ答えが来たなら、**その言い方を覚える**。
        if self.pending_symbol_alias:
            for name in spoken:
                match = __import__(
                    "neuro_voice.games.ktane.symbols", fromlist=["match_symbol"],
                ).match_symbol(name, names, aliases=self.vocabulary.table())
                if match is not None and not match.ambiguous:
                    self.vocabulary.learn(match.name, self.pending_symbol_alias)
                    self.pending_symbol_alias = ""
                    break
        resolved, unknown, ambiguous = resolve_symbols(spoken, names, self.vocabulary)
        known = list(self.slots.get("symbols", []))
        self.slots["symbols"] = known + [s for s in resolved if s not in known]
        if ambiguous:
            self.pending_symbol_alias = ambiguous[0]
            return Solution(
                because=f"「{ambiguous[0]}」がどの記号か決めきれない",
                needs="もう少し特徴（丸か、線が何本か、向き、しっぽがあるか）",
            )
        if unknown:
            self.pending_symbol_alias = unknown[0]
            return Solution(
                because=f"「{unknown[0]}」がどれか分からない",
                needs="どんな形か（丸・線・星・逆さ・しっぽ、みたいに）",
            )
        return rules.solve_keypad(list(self.slots.get("symbols", [])), columns)

    def _teach_keypad_column(self, index: int, symbols: list[str]) -> Solution:
        """「1列目は 丸に線、稲妻、…」で1列ぶん覚える。

        マニュアルを見ながら口で読み上げれば表ができる。**呼び名は
        利用者のもの**なので、こちらで正規化して保存しない。
        """
        if not 1 <= index <= 6:
            return Solution(needs="何列目か（1〜6）")
        columns = [list(column) for column in self.tables.keypad_columns]
        while len(columns) < index:
            columns.append([])
        columns[index - 1] = list(symbols)
        self.tables.keypad_columns = columns
        filled = sum(1 for column in columns if column)
        return Solution(
            f"{index}列目を覚えた。{'、'.join(symbols)}の{len(symbols)}個だね",
            f"{filled}/6列ぶん",
            follow_up=(
                "次の列も教えて" if filled < 6
                else "6列そろった。tools/save_keypad_table.py で保存できるよ"
            ),
        )

    def _solve_whos_on_first(self, text: str) -> Solution | None:
        import re

        display = ""
        display_match = re.search(r"(?:画面|表示|ディスプレイ)[^0-9A-Za-z]{0,6}([A-Za-z' ]{1,12})", text)
        if display_match:
            display = display_match.group(1).strip()
        elif re.search(r"(?:画面|表示)は?(?:何も|空|ブランク|から)", text):
            display = ""
        words = parse_words(text)
        if len(words) >= 6:
            self.who_buttons = words[:6]
        if not display and not self.who_buttons:
            return None
        if not display:
            return Solution(needs="画面に出ている語")
        return rules.solve_whos_on_first(
            display, self.who_buttons,
            self.tables.whos_on_first_positions, self.tables.whos_on_first_order,
        )

    def _solve_maze(self, text: str) -> Solution | None:
        import re

        points = parse_coordinates(text)
        if not points:
            return None
        if re.search(r"丸|まる|マル|circle", text):
            self.maze_circles = points[:2]
        elif re.search(r"四角|白|今いる|現在地|しかく", text):
            self.maze_start = points[0]
        elif re.search(r"三角|赤|出口|ゴール|さんかく", text):
            self.maze_goal = points[0]
        else:
            return Solution(needs="その座標が丸・白い四角・赤い三角のどれか")
        return rules.solve_maze(
            self.maze_circles, self.maze_start, self.maze_goal, self.tables.mazes,
        )

    def _solve_password(self, text: str) -> Solution | None:
        import re

        columns = []
        for chunk in re.findall(r"[a-zA-Z](?:\s*[,、･・]\s*[a-zA-Z]){1,7}", text):
            columns.append(re.findall(r"[a-zA-Z]", chunk))
        if not columns:
            return None
        return rules.solve_password(columns)

    # ------------------------------------------------------------------

    def verify(self, reply: str) -> tuple[str, str]:
        """ポッポの発話を検算する。(判定, 差し替える文) を返す。

        差し替える文が空なら、ポッポの言葉をそのまま話してよい。
        """
        expected, self.pending_check = self.pending_check, None
        if expected is None:
            # **コードが何も答えていないのに操作を指示していないか。**
            # 実機で、表が空のキーパッドに「一番上のプサイを押して」と言って
            # 爆発した。検算は答えがある時しか働かないので、ここで網を張る。
            if verify.contains_instruction(reply):
                return verify.DISAGREE, verify.ungrounded_refusal(self._missing_hint())
            return verify.UNCLEAR, ""
        verdict = verify.check(reply, expected)
        self.agreement_counts[verdict] = self.agreement_counts.get(verdict, 0) + 1
        if verdict == verify.DISAGREE:
            return verdict, verify.correction(expected)
        return verdict, ""

    def _missing_hint(self) -> str:
        """いま何が足りなくて答えられないのか、一言で。"""
        module = self.current_module
        name = MODULE_NAMES.get(module, "")
        if module in NEEDS_TABLE and name in self.tables.missing():
            if module == "keypad":
                return "キーパッドの表がまだ空だから、6列ぶん読み上げて覚えさせて"
            return f"{name}の表をまだ持っていないから、そのモジュールは自分で見て"
        if not self.edge.serial and module in {"wires", "simon", "complicated_wires"}:
            return "シリアル番号を教えて"
        if self.edge.batteries is None and module == "button":
            return "電池の数を教えて"
        if not module:
            return "どのモジュールか教えて"
        return f"{name}について、見えているものをもう一度教えて"

    def rule_prompt(self) -> str:
        """いま扱っているモジュールの規則だけ。全部載せると文脈長を食い潰す。"""
        return rule_text(self.current_module)

    def context(self) -> str:
        """プロンプトへ載せる現状。短く保つ。"""
        lines = [f"【爆弾の状態】{self.edge.summary()}"]
        if self.current_module:
            lines.append(
                f"扱っているモジュール: {MODULE_NAMES.get(self.current_module, self.current_module)}"
            )
        if self.memory_history:
            lines.append(
                "記憶モジュールの履歴: "
                + "、".join(
                    f"{i + 1}段目=左から{p.position}番目({p.label})"
                    for i, p in enumerate(self.memory_history)
                )
            )
        missing = self.tables.missing()
        available = [
            MODULE_NAMES[key] for key in IMPLEMENTED
            if key not in NEEDS_TABLE or MODULE_NAMES[key] not in missing
        ]
        lines.append("規則があるモジュール: " + "、".join(sorted(available)))
        if missing:
            lines.append(
                "表が未取り込みで答えられないモジュール: " + "、".join(missing)
                + "。思いつきで指示しない。"
            )
        return "\n".join(lines)
