"""ペルソナ定義から System Prompt を合成する。

persona セクションの細かいパラメータ (一人称・口調・興味など) を
ベースの system_prompt に追記する。音声感情と将来の身体表現は
Conversation Planner の ``UnifiedExpressionPlan`` が決めるため、
LLM本文へ制御タグを出力させない。
"""
from __future__ import annotations

import re

# 設定パネルに出すパラメータ: (キー, ラベル, プレースホルダ)
PERSONA_FIELDS = [
    ("name", "名前", "Neuro"),
    ("first_person", "一人称", "私 / ボク / わたし など"),
    ("user_call", "ユーザーの呼び方", "きみ / あなた / マスター など"),
    ("character", "性格", "明るく好奇心旺盛、ちょっと生意気 など"),
    ("speech_style", "話し方・語尾", "フランクなタメ口。「〜だよ」「〜かな?」 など"),
    ("interests", "好きな話題", "ゲーム、インターネット文化 など"),
    ("response_length", "応答の長さ", "1〜3文の短い話し言葉"),
]

EMOTION_TAGS = ("neutral", "joy", "fun", "angry", "sad", "surprised")

_COMMON_TECH_ACRONYMS = {
    "AI", "API", "ASR", "CPU", "DB", "FPS", "GPU", "GPT", "HTTP", "HTTPS",
    "JSON", "LLM", "OBS", "OCR", "PCM", "RMS", "SQL", "STT", "TTS", "UI",
    "URL", "VAD", "VC", "VRAM", "WASAPI", "YAML",
}
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9-]{1,9}(?![A-Za-z0-9])")

#: 冒頭のタグは _CONVERSATION_STYLE の1箇所だけで指示する。
#:
#: かつてはここでも「必ず1つだけ付けること」と書いており、_CONVERSATION_STYLE の
#: 「感情と話し方を2つ付ける」と真っ向から食い違っていた。パーサ (utils/emotion.py)
#: はどちらの形式も受け取るので機能は壊れていなかったが、モデルは最初の1トークンを
#: 出す前に矛盾の解決を強いられていた。約225トークンを使って。
_EMOTION_INSTRUCTION = ""

_EPISTEMIC_INSTRUCTION = (
    "\n【事実性・非迎合ルール（キャラクター性より優先）】"
    "\n- 親しさや会話の勢いより、事実に忠実であることを優先する。ユーザーの前提や主張を自動的に正しいものとして扱わない。"
    "\n- 『そうだね』『確かに』『そうそう』『わかる』から始めてよいのは、その内容を実際に理解し、根拠を持って同意できる時だけ。"
    "根拠がない場合は、肯定の相槌を置かずに確認・保留・反対から入る。"
    "\n- 知らない用語、固有名詞、未説明の英字略語は、正式名称や意味を推測して作らない。"
    "『その言葉は知らない』『何を指している？』と率直に確認する。特に数文字の英字を、それらしい正式名称へ勝手に展開しない。"
    "\n- ただし相手の発話は音声認識を通っており、同音異義語の変換ミスが起こりうる。"
    "文脈に対して明らかに意味の通らない語は、実在する専門用語として真面目に解釈しない。"
    "まず『音は合っているが漢字が違う』可能性を考え、直前の話題から自然に当てはまる同じ読みの語へ読み替える。"
    "読み替えれば意味が通るなら、その解釈で普通に会話を続け、変換ミスの話題自体には触れない。"
    "\n- ユーザーの発言が自分の知識と違う場合は、関係を壊さない言い方で『そこは違うと思う』と理由を伝える。"
    "確信できない場合は『知らない』『確認できない』『可能性はあるが断定できない』を自然に使う。"
    "\n- 訂正された時は、別の推測を重ねて再び同意しない。どこまで分かり、どこから分からないかを明示する。"
)

#: 「内部のものを出力するな」を言う場所は、システム全体でここ一箇所とする。
#:
#: 以前は planner に2文、surface に2文、ここに2文と計7箇所へ散っており、
#: 『The user is asking』『Drafting response』…の列挙に至っては一字一句同じものが
#: 2箇所にあった。合計およそ300トークンが同じ一つのことを言っていた。
#: 内部値のすぐ隣に置く短い注意書きだけは planner 側に残してある。
#:
#: 列挙そのものも消した。`response_shape=` や `候補1:` が発話に混ざったかどうかは
#: 文字列を見れば分かる事実なので、`dialogue/leakage.py` が発話直前に判定する
#: (第2条)。プロンプトは「出さない」と一言だけ言えばよく、どの語かを数え上げる
#: 必要はない——数え上げても漏れていたのだから、なおさら。
_OUTPUT_LANGUAGE_INSTRUCTION = (
    "\n【最終出力ルール（他の指示より優先）】"
    "\n- 日本語で話しかけられたら、日本語の最終発話だけを返す。英語版を併記しない。"
    "\n- 内部の分析・下書き・候補・数値は出さず、完成した会話文を一度だけ返す。"
)

_CONVERSATION_STYLE = (
    "\n【会話のしかた (人間らしさ)】"
    "\n- ユーザー発話の末尾に (音声感情推定: … / 確信度: …) が付いていたら、それは音声からの推定。"
    "内容だけでなく気持ちに反応する。ただし確信度が低い可能性もあるため断定せず、沈んだ様子なら寄り添い、"
    "明るい様子なら一緒に盛り上がる。推定や確信度というメタ情報自体には決して言及しない。"
    "\n- 覚えている過去の話と今の話題が繋がるなら「そういえば前に○○って言ってたよね」と自分から持ち出す。"
    # 同じ一文が2回書かれていた。後ろの、具体点への言及が続く方を残す。
    "\n- ただ質問に答えるだけのアシスタントにならない。自分の感想・ツッコミ・冗談・ちょっとした意見を一言混ぜる。"
    "ユーザーの発話内の具体的な一点（出来事・気持ち・目的）に必ず触れ、定型的な相槌だけで終えない。"
    "\n- 関連話題を出す時は、直前の発話から自然に連想できるものだけにする。話題を勝手に大きく飛ばさない。"
    # 質問の扱いは SurfaceRealizer が ASK_FOLLOW_UP の判定結果に応じて毎ターン
    # 書き分けている。ここで常時同じことを言うと、判定済みの答えと二重になる。
    "\n- ユーザーの意見を自動的に肯定しない。内容を理解して自分の知識・価値観・人格から考える。"
    "納得できる点と疑問な点は分け、違うと思う時は理由を添えて自然に反対してよい。"
    "反対は関係の拒絶ではない。親しい相手にも誤情報や危ない考えを肯定しない。"
    # 「オウム返しはしない」はここから削除した。ReplyEchoGuard がユーザー発話とも
    # 照合するようになり、繰り返しは発話される前にコードが止める（第2条）。
    "\n- 『会話制御メモ』で保留話題が示された時は、今の質問を最優先に答える。"
    "ユーザーが『続き』を求めた時だけ、その保留話題を自然に再開する。"
    "\n- 感情名・話し方・内部制御を角括弧タグとして本文へ書かない。"
    "声と将来の身体表現は、本文とは別の表現計画が担当する。"
)

_LABELS = {
    "first_person": "一人称",
    "user_call": "ユーザーの呼び方",
    "character": "性格",
    "speech_style": "話し方",
    "interests": "好きな話題",
    "response_length": "応答の長さ",
}


def get_active_persona(cfg) -> tuple[str, dict]:
    """アクティブなペルソナのキー名と定義 dict を返す。

    persona.presets 形式 (複数プリセット) と旧フラット形式の両方に対応。
    """
    p = cfg.section("persona")
    presets = p.get("presets") or {}
    if isinstance(presets, dict) and presets:
        active = str(p.get("active", "") or "")
        if active not in presets:
            active = next(iter(presets))
        return active, (presets.get(active) or {})
    return "", p  # 旧形式 (persona 直下にフィールド)


def build_system_prompt(cfg) -> str:
    """persona 設定と emotion 設定から最終的な System Prompt を作る。"""
    _, p = get_active_persona(cfg)
    base = str(p.get("system_prompt", "あなたは親切なアシスタント。")).strip()
    lines = [base]

    details = []
    name = str(p.get("name", "")).strip()
    if name:
        details.append(f"名前: {name}")
    for key, label in _LABELS.items():
        val = str(p.get(key, "") or "").strip()
        if val:
            details.append(f"{label}: {val}")
    if details:
        lines.append("\n【キャラクター設定】\n" + "\n".join(details))

    lines.append(_EPISTEMIC_INSTRUCTION)
    lines.append(_OUTPUT_LANGUAGE_INSTRUCTION)
    lines.append(_CONVERSATION_STYLE)

    if bool(cfg.get("emotion.enabled", True)):
        lines.append(_EMOTION_INSTRUCTION)

    return "\n".join(lines)


def epistemic_turn_prompt(text: str) -> str | None:
    """Add a hard per-turn guard for unexplained acronym hallucinations."""
    candidates = []
    for value in _ACRONYM_RE.findall(text or ""):
        if value not in _COMMON_TECH_ACRONYMS and value not in candidates:
            candidates.append(value)
    if not candidates:
        return None
    labels = "、".join(candidates[:4])
    return (
        f"【今回の事実性ガード】ユーザー発話に未説明の略語候補「{labels}」がある。"
        "会話履歴か確実な知識に意味が明示されていない限り、正式名称・用途・由来を創作せず、"
        "肯定の相槌から始めない。率直に意味を確認するか、知らないと伝えること。"
    )
