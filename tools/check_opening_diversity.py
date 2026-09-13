"""同じ入力に同じ書き出しが返る件を、温度を変えながら測る。

## これまでに分かっていること

`docs/CONVERSATION_AB_SCRIPT.md` の8文を4回流して、冒頭ハッシュが毎回同じだった。

* Planner（738tok）を丸ごと外しても変わらない
* 常時ONのプロンプトを 3642→2899tok にしても変わらない
* 会話履歴が違っても変わらない
* **裸のプロンプトなら同じ入力3回で3種類に割れる**（`check_sampling.py`）

つまりサンプリングは壊れていない。**アプリの巨大なプロンプトが分布を
尖らせている**。そして `llm.temperature = 0.7` は分布を**さらに尖らせる**
向きの値である。Gemma 4 自身の推奨は temperature 1.0 / top_k 64 / top_p 0.95。

温度は今日まで一度も触っていない唯一のつまみで、しかも「ばらつき」を
直接決める量である。効くかどうかは測れば分かる。

## 測り方

各温度で、8文の台本を最初から複数回流す。**seedは指定しない**（実運用と
同じ確率的サンプリング）。見るのは2つ。

* **位置ごとの書き出しの種類**  同じ入力に何通りの入り方をしたか。
  実測の症状はここが常に1だった
* **走行内の冒頭重複率**  1回の会話の中で同じ入り方を繰り返していないか

書き出しだけ見るので `num_predict` は小さくてよい。本文は保存も表示もしない。

    python tools/check_opening_diversity.py

温度を変えたい時は `TEMPERATURES` を編集する。
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
from neuro_voice.dialogue.plan_metrics import opening_key
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
TEMPERATURES = (0.7, 1.0)
RUNS = 3
#: 書き出しだけ見るので短くてよい。全文を出させると1回あたり数倍かかる。
PREDICT = 48
BASE = "http://localhost:11434"


def post(path: str, body: dict, timeout: float = 180.0) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


#: 想起された過去の会話が、実アプリでどう見えるか（`mind.py` の
#: `transcript_influences` に合わせた近似）。ここでは仕組みを試すためだけに使う。
RECALL_TEMPLATE = (
    "【思い出した会話】\n"
    "チビ: {user} / 私: {assistant}\n"
    "記録に無い部分は曖昧にごまかさない。"
)


def run_once(
    cfg: Config, temperature: float, recall: list[str] | None = None,
    carried: list[dict[str, str]] | None = None, kernel=None,
) -> tuple[list[str], float | None, list[str], list[dict[str, str]]]:
    """台本を1回流し、位置ごとの冒頭ハッシュと走行内の重複率を返す。

    `recall` を渡すと、各ターンで**そのターンの過去の返答**をプロンプトへ
    混ぜる。実アプリの意味検索が同じ入力に対する過去の自分の答えを
    引いてくる状況を、この経路で再現するため。
    """
    persona_key = str(cfg.get("persona.active", "") or "")
    model = str(cfg.get("llm.backends.ollama.model", "gemma4:12b-it-qat"))
    system_prompt = build_system_prompt(cfg)
    # `carried` を渡すと、前の走行の会話をそのまま引き継ぐ。実機で同じ台本を
    # 続けて流した状況（＝同一セッション内の繰り返し）を再現するため。
    history: list[dict[str, str]] = list(carried or [])
    openings: list[str] = []
    replies: list[str] = []
    last_window: dict = {}
    with TemporaryDirectory() as tmp:
        dialogue = DialogueIntelligence(
            cfg, Path(tmp) / "dialogue.json", persona_key=persona_key,
        )
        for index, user_text in enumerate(SCRIPT, 1):
            dialogue.observe_turn("diversity-user", user_text, now=1000 + index * 10)
            context = dialogue.prompt_context("diversity-user", user_text)
            kernel_block = []
            if kernel is not None:
                frame = kernel.build(
                    user_text, source="local", speaker_id="diversity-user",
                    momentum=0.5, conversation_id="diversity-user",
                )
                kernel_block = [{"role": "system", "content": kernel.prompt(frame)}]
            recalled = []
            if recall and index <= len(recall):
                recalled = [{"role": "system", "content": RECALL_TEMPLATE.format(
                    user=user_text[:80], assistant=recall[index - 1][:100],
                )}]
            data = post("/v1/chat/completions", {
                "model": model,
                "messages": [
                    *kernel_block,
                    {"role": "system", "content": system_prompt},
                    {"role": "system", "content": context},
                    *recalled,
                    *history[-10:],
                    {"role": "user", "content": user_text},
                ],
                "temperature": temperature,
                "max_tokens": PREDICT,
                "reasoning_effort": "none",
                # **seedは指定しない。** 実運用と同じ確率的サンプリングを見る。
                "options": {
                    "num_ctx": int(cfg.get("llm.num_ctx", 8192)),
                    "num_predict": PREDICT,
                },
            })
            reply = str(data["choices"][0]["message"]["content"]).strip()
            if not reply:
                raise RuntimeError(f"turn {index}: 本文が空")
            openings.append(opening_key(reply))
            replies.append(reply)
            dialogue.record_response("diversity-user", reply)
            last_window = dict(dialogue.last_plan_metrics.get("window", {}))
            history.extend((
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ))
            print(".", end="", flush=True)
        dialogue.close()
    return openings, last_window.get("opening_repeat_ratio"), replies, history


def report(temperature: float, runs: list[list[str]], ratios: list[float | None]) -> float:
    """位置ごとに何種類の書き出しが出たか。1なら毎回同じ。"""
    per_position = [
        len({run[index] for run in runs}) for index in range(len(SCRIPT))
    ]
    average = sum(per_position) / len(per_position)
    stuck = sum(1 for count in per_position if count == 1)
    known = [value for value in ratios if isinstance(value, (int, float))]
    print(f"\n[temperature={temperature}]")
    print("  位置ごとの書き出しの種類: " + " ".join(str(count) for count in per_position))
    print(f"  平均 {average:.2f} 種類 / {len(runs)} 回中  —  毎回同じだった位置: {stuck}/{len(SCRIPT)}")
    if known:
        print(f"  走行内の冒頭重複率(平均): {sum(known) / len(known):.3f}")
    return average


def recall_arm(cfg: Config, temperature: float) -> None:
    """過去の自分の返答をプロンプトへ混ぜると、書き出しが固まるか。

    2026-08-01の実測で、この経路（persona + 対話制御 + 履歴）は温度0.7でも
    2.75/3種類に散った。ところが**実アプリは4回とも8/8同じ**だった。
    差分は `Mind.build_context` が足すもの——記憶の想起、会話ログの参照、
    関係性、時刻——である。

    そのうち「同じ入力に対する過去の自分の答え」が入ると、モデルはそれを
    写す。写せば当然、毎回同じになる。仕組みとして本当にそうなるかを、
    ここで直接確かめる。
    """
    print("\n[想起あり] 1回目の返答を、2回目以降のプロンプトへ混ぜる")
    first, _ratio, replies, _history = run_once(cfg, temperature)
    repeated = 0
    total = 0
    for _ in range(max(1, RUNS - 1)):
        openings, _ratio, _replies, _history = run_once(cfg, temperature, recall=replies)
        for before, after in zip(first, openings):
            total += 1
            repeated += int(before == after)
    share = repeated / total if total else 0.0
    print(f"  過去の書き出しをなぞった割合: {repeated}/{total} ({share:.0%})")
    return share


def kernel_arm(cfg: Config, temperature: float) -> float:
    """Conversation Kernel を足すと書き出しが固まるか。**最有力の容疑者。**

    実アプリのプロンプトで最大のブロックは
    `[CONVERSATION KERNEL - authoritative decision for this turn]`（約4300tok、
    persona 1861 より大きい）で、この経路には**一度も入っていなかった**。
    `dialogue.prompt_context` ではなく `mind._conversation_kernel_context` が
    作るものだから。

    中身の `response_goal` と `response_shape` は
    `kernel.py:536-565` を見ると**ユーザーの発話だけで決まる決定的な値**で、
    しかも「権威ある決定」と名乗って具体的な命令文を渡している。

        「うんうん」「なるほど」  → shape=brief
        「〜必要ある？」          → shape=answer_first,
                                    goal="Answer the question directly ..."

    同じ入力なら毎回まったく同じ一文が入る。Plannerの抽象的なスタイル名と
    違って具体的なので、外しても変わらなかったのと辻褄が合う。
    """
    from neuro_voice.dialogue.kernel import ConversationKernel

    print("\n[Kernelあり] 実アプリ最大のブロックを足す")
    kernel = ConversationKernel()
    runs: list[list[str]] = []
    for _ in range(RUNS):
        openings, _ratio, _replies, _history = run_once(
            cfg, temperature, kernel=kernel,
        )
        runs.append(openings)
    per_position = [len({run[i] for run in runs}) for i in range(len(SCRIPT))]
    stuck = sum(1 for count in per_position if count == 1)
    print("  位置ごとの書き出しの種類: " + " ".join(str(c) for c in per_position))
    print(f"  毎回同じだった位置: {stuck}/{len(SCRIPT)}")
    return stuck / len(SCRIPT)


def same_session_arm(cfg: Config, temperature: float) -> float:
    """同じセッションの中で台本を繰り返す。**実機でやったのはこれ。**

    実機の受入は、同じ8文を続けて4回流していた。2回目以降のプロンプトには、
    **直前の会話履歴に一字一句同じ質問とその答えが残っている**。
    想起として1件混ぜるのとは強さが違う。

    まっさらから始める計測（0/8が詰まる）と、この計測（実機は8/8詰まった）の
    差がそのまま原因である。
    """
    print("\n[同一セッション] 会話履歴を引き継いだまま台本を繰り返す（実機と同じ）")
    first, _ratio, _replies, history = run_once(cfg, temperature)
    repeated = 0
    total = 0
    for round_index in range(2, RUNS + 1):
        openings, _ratio, _replies, history = run_once(
            cfg, temperature, carried=history,
        )
        matched = sum(int(a == b) for a, b in zip(first, openings))
        repeated += matched
        total += len(first)
        print(f"  {round_index}周目: 1周目と同じ書き出し {matched}/{len(first)}")
    share = repeated / total if total else 0.0
    print(f"  合計 {repeated}/{total} ({share:.0%})")
    return share


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    cfg = Config.load(ROOT / "config" / "config.yaml")
    print(f"台本{len(SCRIPT)}文 × {RUNS}回 × 温度{len(TEMPERATURES)}種類")
    print("seedは指定しない。書き出しだけ見るので本文は保存しない。\n")
    started = time.perf_counter()
    averages: dict[float, float] = {}
    try:
        for temperature in TEMPERATURES:
            runs, ratios = [], []
            for _ in range(RUNS):
                openings, ratio, _replies, _history = run_once(cfg, temperature)
                runs.append(openings)
                ratios.append(ratio)
            averages[temperature] = report(temperature, runs, ratios)
        best = max(averages, key=lambda key: averages[key]) if averages else 0.7
        # この経路で既に散っているなら、温度は原因ではない。実アプリとの
        # 差分を2つ試す。想起として1件混ぜる場合と、実機と同じく会話履歴を
        # 引き継いだまま繰り返す場合。
        recalled = same_session = None
        if averages and averages[best] >= 2.0:
            kernelled = kernel_arm(cfg, TEMPERATURES[0])
            recalled = recall_arm(cfg, TEMPERATURES[0])
            same_session = same_session_arm(cfg, TEMPERATURES[0])
            print("\n=== 切り分け（詰まった割合。実機は 100%） ===")
            print("  まっさらから毎回      :   0%（上の表）")
            print(f"  想起を1件混ぜる       : {recalled:>3.0%}")
            print(f"  会話履歴を引き継ぐ    : {same_session:>3.0%}")
            print(f"  **Kernelを足す**      : {kernelled:>3.0%}  ← 実アプリ最大のブロック")
            if kernelled >= 0.6:
                print(
                    "\n  → **これが原因**。`response_goal` と `response_shape` は\n"
                    "     ユーザーの発話だけで決まる決定的な値で、しかも\n"
                    "     「このターンの権威ある決定」として具体的な命令文で渡る。\n"
                    "     同じ入力なら毎回同じ一文。温度でもプロンプトの量でも\n"
                    "     Plannerでもなかった。"
                )
            elif same_session >= 0.6 or recalled >= 0.6:
                print("\n  → Kernel以外が効いている。上の数字の高い方を追うこと。")
            else:
                print(
                    "\n  → まだ実機の100%を説明しない。`Mind.build_context` の\n"
                    "     残り（記憶・関係性・時刻・会話ログ）を1つずつ足すこと。"
                )
    except (urllib.error.URLError, RuntimeError, KeyError) as error:
        print(f"\n計測を完了できませんでした: {error}")
        return 1

    print(f"\n所要時間: {time.perf_counter() - started:.1f}s")
    top = max(averages, key=lambda key: averages[key]) if averages else None
    if top is None:
        return 0
    if averages[top] < 1.5:
        print(
            "\n→ どの温度でも書き出しが固まった。この経路だけで再現している。\n"
            "  persona と対話制御のプロンプトを削る方へ回すこと。"
        )
    elif averages[top] - averages.get(TEMPERATURES[0], 0) > 0.4:
        print(
            f"\n→ temperature={top} で明確にばらつきが増えた（{averages[top]:.2f}種類）。\n"
            f"  `llm.temperature` を {top} にしてよいが、**ばらつきと的確さは\n"
            "  引き換え**なので、実機で受け答えの質を必ず確かめること。"
        )
    else:
        print(
            "\n→ **この経路では症状が再現しない**（既に十分ばらついている）。\n"
            "  温度は原因ではない。原因は実アプリだけが足しているもの——\n"
            "  `Mind.build_context`（記憶の想起・会話ログ・関係性・時刻）にある。\n"
            "  上の[想起あり]の結果を見ること。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
