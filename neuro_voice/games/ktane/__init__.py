"""Keep Talking and Nobody Explodes の解法をローカルに持つ。

ポッポは「マニュアル担当」を名乗っていたのに、プロファイルに入っていたのは
役割の説明と版番号と検証コードだけで、**規則が1行も無かった**。
だから何を説明されても指示が出せず、質問を返すことしかできなかった。

判断はコードが行い、モデルは確定した答えを自分の言葉で伝える（第2条）。
12Bのモデルに多条件の分岐を解かせて間違えると、爆弾は爆発する。

自信のない規則は実装しない。`Solution.unknown` で「手元に規則がない」と
言わせる。間違った指示は「分からない」よりはるかに悪い。
"""
from neuro_voice.games.ktane.edgework import Edgework
from neuro_voice.games.ktane.expert import KtaneExpert, MODULE_NAMES
from neuro_voice.games.ktane.modules import Solution

__all__ = ["Edgework", "KtaneExpert", "MODULE_NAMES", "Solution"]
