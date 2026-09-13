"""同じ入力を3回送って、返答がばらつくかどうかだけを見る。

なぜこれが要るか。

`docs/CONVERSATION_AB_SCRIPT.md` の8文を **4回** 流した結果、書き出しが
毎回同じ位置で同じだった。Planner有効/無効、プロンプト −743tok、会話履歴の
違い——どれも書き出しを動かさなかった。`llm.temperature` は 0.7 で seed も
指定していないのに、である。

温度0.7のサンプリングが効いていれば、少なくとも数回に一度は書き出しが
変わるはず。変わらないなら、原因は「モデルが指示に従わない」ではなく
**サンプリングが効いていない**ことになり、直す場所がまったく違う。

アプリを起動したままで実行できる。会話履歴もペルソナも使わない。

    python tools/check_sampling.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:11434"
MODEL = "gemma4:12b-it-qat"
PROMPT = "今のAIって話しててもAI感が強いんだよね"
ROUNDS = 3


def post(path: str, body: dict, timeout: float = 120.0) -> dict:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def show() -> None:
    """モデル自身が持っている既定値。seed や temperature 0 が潜んでいないか。"""
    try:
        data = post("/api/show", {"model": MODEL}, timeout=30)
    except Exception as error:                                  # pragma: no cover
        print(f"  /api/show に失敗: {error}")
        return
    parameters = str(data.get("parameters") or "").strip()
    print(parameters or "  (PARAMETER の指定なし)")


def openai_style(temperature: float, *, send_options: bool) -> list[str]:
    """アプリとまったく同じ経路（/v1/chat/completions）。"""
    out = []
    for _ in range(ROUNDS):
        body: dict = {
            "model": MODEL,
            "messages": [{"role": "user", "content": PROMPT}],
            "temperature": temperature,
            "max_tokens": 120,
            # OpenAICompatBackend.request_extra_body() と同じ指定。
            # これがないとGemma 4は120 tokenを内部思考だけで使い切り、
            # content=""を「同じ返答」と誤判定してしまう。
            "reasoning_effort": "none",
        }
        if send_options:
            # config の llm.num_ctx と揃えておく。ここだけ古いと
            # 「アプリと同一経路」の看板が嘘になる。
            body["options"] = {"num_ctx": 16384, "num_predict": 1024}
        data = post("/v1/chat/completions", body)
        out.append(str(data["choices"][0]["message"]["content"]).strip())
    return out


def native(temperature: float) -> list[str]:
    """Ollama自身のAPI。OpenAI互換層を疑うための対照。"""
    out = []
    for _ in range(ROUNDS):
        data = post("/api/chat", {
            "model": MODEL, "stream": False,
            # OpenAI互換側の reasoning_effort=none と同じ対照条件。
            "think": False,
            "messages": [{"role": "user", "content": PROMPT}],
            "options": {"temperature": temperature, "num_predict": 120},
        })
        out.append(str(data["message"]["content"]).strip())
    return out


def report(label: str, replies: list[str]) -> None:
    if not all(replies):
        print(f"\n[{label}] 本文が空のため判定不能")
        return
    unique = len({reply[:24] for reply in replies})
    verdict = "ばらつきあり" if unique > 1 else "★全部同じ"
    print(f"\n[{label}] 書き出しの種類 {unique}/{len(replies)} - {verdict}")
    for index, reply in enumerate(replies, 1):
        print(f"  {index}: {reply[:48]}")


def main() -> int:
    # Windowsの既定CP932でモデル出力に未対応文字が含まれても、診断全体を
    # UnicodeEncodeErrorで中断しない。
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    print(f"モデル: {MODEL} / 同じ入力を{ROUNDS}回")
    print("\n=== モデル自身の既定パラメータ ===")
    show()
    try:
        report("OpenAI互換 temp=0.7 + options（アプリと同一）",
               openai_style(0.7, send_options=True))
        report("OpenAI互換 temp=0.7 optionsなし",
               openai_style(0.7, send_options=False))
        report("OpenAI互換 temp=1.0", openai_style(1.0, send_options=False))
        report("ネイティブAPI temp=0.7", native(0.7))
    except urllib.error.URLError as error:
        print(f"\nOllamaへ接続できませんでした: {error}")
        return 1
    print(
        "\n読み方:\n"
        "  どれも『全部同じ』  → サンプリングが効いていない。プロンプトの問題ではない\n"
        "  temp=1.0 だけ割れる → 0.7がこのモデルには低すぎる。設定で直せる\n"
        "  ネイティブだけ割れる → OpenAI互換層が temperature を落としている\n"
        "  どれも割れる        → サンプリングは正常。原因は入力側にある"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
