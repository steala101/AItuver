"""内省 (reflection) 用プロンプトと、LLM出力JSONの寛容なパース。"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

REFLECTION_SYSTEM = (
    "あなたはAIキャラクター「{name}」の内面を管理する裏方モジュール。"
    "会話ログを読み、記憶すべきことと内面の小さな変化をJSONだけで出力する。"
    "説明文・コードブロック記号は一切出力しない。"
)

REFLECTION_TEMPLATE = """## 現在の内面状態
{state}

## 現在のセッション要約 (これまで)
{summary}

## 直近の会話ログ
{turns}

## 指示
上記から次のJSONを1つだけ出力せよ:
{{
  "summary": "セッション要約を更新した1〜3文 (日本語)",
  "memories": [
    {{"text": "ユーザーに関する事実や大事な出来事 (簡潔な1文)", "kind": "fact", "importance": 1-5}}
  ],
  "trait_deltas": {{"brightness": 0.0, "curiosity": 0.0, "empathy": 0.0, "mischief": 0.0, "confidence": 0.0, "openness": 0.0}},
  "like_updates": [
    {{"topic": "話題名 (短く)", "delta": -0.15〜0.15, "reason": "短い理由"}}
  ],
  "mood": {{"valence": -1.0〜1.0, "arousal": -1.0〜1.0}},
  "relationship_events": [
    {{"speaker_id": "会話ログにある相手ID", "event_type": "kindness|empathy|honesty|consistency|shared_interest|thoughtful_disagreement|respectful_boundary|vulnerability|support|humor_connection|dismissal|disrespect|hostility|deception|coercion|boundary_violation|mockery|repeated_interruption|manipulation|unfair_blame|apology|repair_attempt|behavior_improvement|clarification", "intensity": 0.0〜1.0, "confidence": 0.0〜1.0, "summary": "会話全文を写さない短い理由"}}
  ],
  "note": "内面が変化した理由を短い一言で (変化がなければ空文字)",
  "speaker_name": "話し相手が会話中に自分の名前を名乗った場合のみその名前。それ以外は空文字"
}}

ルール:
- memories は本当に覚える価値があるものだけ (0〜4件)。名前・好み・予定・関係の変化は重要度高め。
  誰の情報か分かるように「(相手の名前)は〜」の形で書く。
- trait_deltas は会話が性格に与えた影響。ほとんどの会話では 0 か ±0.01 程度。強い体験のみ ±0.03。
- like_updates は会話で触れた話題への好感の変化。新しい話題への興味も可 (0〜3件)。
- relationship_events は実際の会話行動だけを評価する (0〜4件)。相手の自己申告だけで trust を上げない。
  意見の違いそのものは悪い出来事ではない。敬意を持つ反対は thoughtful_disagreement にできる。
  単発の失礼は低強度にし、同じパターンが続く場合だけ強める。謝罪・説明・改善は repair 系として記録する。
- JSON以外を出力しない。"""


def build_reflection_messages(
    name: str, state: str, summary: str, turns: list[dict[str, str]]
) -> list[dict[str, str]]:
    log_lines = []
    for t in turns:
        who = t.get("speaker") or "ユーザー"
        key = str(t.get("speaker_key", "") or "")
        log_lines.append(f"{who} [相手ID:{key}]: {t['user']}")
        log_lines.append(f"{name}: {t['assistant']}")
    return [
        {"role": "system", "content": REFLECTION_SYSTEM.format(name=name)},
        {
            "role": "user",
            "content": REFLECTION_TEMPLATE.format(
                state=state,
                summary=summary or "(まだなし)",
                turns="\n".join(log_lines) or "(なし)",
            ),
        },
    ]


def parse_reflection_json(text: str) -> dict[str, Any] | None:
    """LLM出力からJSONを寛容に取り出す。失敗したら None。"""
    if not text:
        return None
    # コードブロックを剥がす
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    raw = text[start : end + 1]
    for attempt in (raw, re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            data = json.loads(attempt)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    logger.warning("内省JSONのパースに失敗: %.200s", raw)
    return None
