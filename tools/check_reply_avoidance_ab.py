"""直近の実返答を使う重複回避を、同じ8ターンでA/B比較する。

会話本文はファイルにも標準出力にも残さない。各armは別の一時的な
DialogueIntelligence状態を使い、同じseed列でOllamaへ送る。比較対象は
``opening_repeat_ratio`` など既存のハッシュ指標だけ。

    python tools/check_reply_avoidance_ab.py
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from neuro_voice.dialogue import DialogueIntelligence
from neuro_voice.memory.persona import build_system_prompt
from neuro_voice.utils.config import Config


SCRIPT = (
    "なんというか、説明しにくいんだけど、今のAIって話しててもAI感が強いんだよね",
    "うんうん",
    "なるほど",
    "Conversation Stateは毎回LLMで更新する必要ある？",
    "またAIが「なるほど、それは重要ですね」って言ってる",
    "人間味を出すなら、ランダムに失敗させればいいと思う",
    "さっきのAI感の話だけど、結局どこが一番効くと思う？",
    "そうだね",
)
BASE = "http://localhost:11434"


def post(path: str, body: dict, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run_arm(cfg: Config, *, enabled: bool) -> dict:
    cfg.set(
        "dialogue.conversation_generation.recent_reply_avoidance.enabled",
        enabled,
    )
    persona_key = str(cfg.get("persona.active", "") or "")
    model = str(cfg.get("llm.backends.ollama.model", "gemma4:12b-it-qat"))
    temperature = float(cfg.get("llm.temperature", .7))
    system_prompt = build_system_prompt(cfg)
    history: list[dict[str, str]] = []
    last_window: dict = {}
    with TemporaryDirectory() as tmp:
        dialogue = DialogueIntelligence(
            cfg, Path(tmp) / "dialogue.json", persona_key=persona_key,
        )
        for index, user_text in enumerate(SCRIPT, 1):
            dialogue.observe_turn("ab-user", user_text, now=1000 + index * 10)
            context = dialogue.prompt_context("ab-user", user_text)
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": context},
                *history[-10:],
                {"role": "user", "content": user_text},
            ]
            data = post("/v1/chat/completions", {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 220,
                "reasoning_effort": "none",
                "options": {
                    "num_ctx": int(cfg.get("llm.num_ctx", 8192)),
                    "num_predict": 512,
                    "seed": 7300 + index,
                },
            })
            reply = str(data["choices"][0]["message"]["content"]).strip()
            if not reply:
                raise RuntimeError(f"turn {index}: Ollama returned empty content")
            dialogue.record_response("ab-user", reply)
            last_window = dict(dialogue.last_plan_metrics.get("window", {}))
            history.extend((
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ))
            print(f"  turn {index}/{len(SCRIPT)} complete", flush=True)
        guidance = dialogue.snapshot("ab-user")["conversation_generation"].get(
            "recent_reply_guidance", {},
        )
        dialogue.close()
    return {
        "recent_reply_avoidance": enabled,
        "guidance_examples": int(guidance.get("examples", 0) or 0),
        "opening_repeat_ratio": last_window.get("opening_repeat_ratio"),
        "ending_repeat_ratio": last_window.get("ending_repeat_ratio"),
        "question_ratio": last_window.get("question_ratio"),
        "length_mean": last_window.get("length_mean"),
        "length_stdev": last_window.get("length_stdev"),
        "planned_length_distribution": last_window.get("planned_length_distribution"),
        "shape_distribution": last_window.get("shape_distribution"),
        "style_distribution": last_window.get("style_distribution"),
    }


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    cfg = Config.load(ROOT / "config" / "config.yaml")
    started = time.perf_counter()
    try:
        print("A: recent_reply_avoidance=OFF")
        control = run_arm(cfg, enabled=False)
        print("B: recent_reply_avoidance=ON")
        treatment = run_arm(cfg, enabled=True)
    except (urllib.error.URLError, RuntimeError, KeyError) as error:
        print(f"A/Bを完了できませんでした: {error}")
        return 1
    print("\n=== 本文を保存しないA/B結果 ===")
    print(json.dumps(
        {"control": control, "treatment": treatment},
        ensure_ascii=False, indent=2,
    ))
    before = control.get("opening_repeat_ratio")
    after = treatment.get("opening_repeat_ratio")
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        verdict = "改善" if after < before else ("悪化" if after > before else "差なし")
        print(f"\n冒頭重複率: {before} -> {after} ({verdict})")
    print(f"所要時間: {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
