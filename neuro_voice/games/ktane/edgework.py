"""爆弾そのものの情報。どのモジュールを解くにも先に要る。

シリアル番号、電池の数、点灯インジケーター、ポート。KTANEの規則の多くは
「シリアルの末尾が奇数なら」「電池が2個以上なら」といった形でここを参照する。

ポッポは画面を見ないので、この値はすべてユーザーから聞いた申告である。
**聞いていない項目を既定値で埋めない。** 電池の数を知らないまま「2個未満だから」
と判断すると、間違った線を切らせる。知らないものは `None` のままにして、
必要になった時に一つだけ尋ねる。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 公式マニュアルに出てくる点灯インジケーターのラベル。
INDICATOR_LABELS = (
    "SND", "CLR", "CAR", "IND", "FRQ", "SIG", "NSA", "MSA", "TRN", "BOB", "FRK",
)
#: ポートの種類（日本語の呼び方はパーサ側で吸収する）。
PORT_KINDS = ("DVI-D", "Parallel", "PS/2", "RJ-45", "Serial", "Stereo RCA")

_VOWELS = frozenset("AEIOU")


@dataclass(slots=True)
class Edgework:
    """聞き取れた範囲の爆弾情報。未確認の項目は None のまま。"""

    serial: str = ""
    batteries: int | None = None
    #: 点灯しているインジケーターのラベル。消灯は別に持つ。
    lit_indicators: set[str] = field(default_factory=set)
    unlit_indicators: set[str] = field(default_factory=set)
    ports: set[str] = field(default_factory=set)
    strikes: int = 0

    # -- シリアル番号から引ける事実 -----------------------------------

    @property
    def serial_last_digit(self) -> int | None:
        for char in reversed(self.serial):
            if char.isdigit():
                return int(char)
        return None

    @property
    def serial_last_digit_is_odd(self) -> bool | None:
        digit = self.serial_last_digit
        return None if digit is None else digit % 2 == 1

    @property
    def serial_has_vowel(self) -> bool | None:
        if not self.serial:
            return None
        return any(char in _VOWELS for char in self.serial.upper())

    # -- 問い合わせ -----------------------------------------------------

    def lit(self, label: str) -> bool:
        return label.upper() in self.lit_indicators

    def has_port(self, kind: str) -> bool:
        return kind in self.ports

    def known(self, name: str) -> bool:
        """その項目を実際に聞けているか。既定値と「未確認」を混同しないため。"""
        if name == "serial":
            return bool(self.serial)
        if name == "batteries":
            return self.batteries is not None
        if name == "indicators":
            return bool(self.lit_indicators or self.unlit_indicators)
        if name == "ports":
            return bool(self.ports)
        return False

    def summary(self) -> str:
        """プロンプトへ載せる短い現状。未確認は「未確認」と書く。"""
        parts = [f"シリアル={self.serial or '未確認'}"]
        parts.append(f"電池={self.batteries if self.batteries is not None else '未確認'}")
        parts.append(
            "点灯=" + ("、".join(sorted(self.lit_indicators)) if self.lit_indicators else "未確認")
        )
        parts.append("ポート=" + ("、".join(sorted(self.ports)) if self.ports else "未確認"))
        parts.append(f"ミス={self.strikes}")
        return " / ".join(parts)
