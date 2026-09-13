"""口で教えたキーパッドの表を、次回も使えるように保存する。

会話で教えた表は、そのセッションのメモリにしか無い。再起動すると消える。
`logs/neuro_voice.log` に残った「N列目を覚えた」から拾い直して
`manual/tables/keypad.json` へ書く。

    python tools/save_keypad_table.py

**呼び名は利用者のもの**なので、こちらで整形しない。読み上げたとおりに保存する。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG = ROOT / "logs" / "neuro_voice.log"
TABLE = (
    ROOT / "game_profiles" / "keep_talking_and_nobody_explodes"
    / "manual" / "tables" / "keypad.json"
)
#: 「3列目を覚えた。丸に線、稲妻、…の5個だね」
_TAUGHT = re.compile(r"(\d)列目を覚えた。(.+?)の\d+個だね")


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    if not LOG.exists():
        print(f"ログが見つかりません: {LOG}")
        return 1
    columns: dict[int, list[str]] = {}
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        for index, body in _TAUGHT.findall(line):
            # 同じ列を言い直したら、**後の方を採る**。言い直しは訂正である。
            columns[int(index)] = [s.strip() for s in body.split("、") if s.strip()]
    if not columns:
        print("覚えた列がログに見つかりません。先に会話で読み上げてください:")
        print("  「キーパッドの1列目は 丸に線、稲妻、逆さのC、…」")
        return 1

    ordered = [columns.get(i, []) for i in range(1, max(columns) + 1)]
    data = json.loads(TABLE.read_text(encoding="utf-8")) if TABLE.exists() else {}
    data["columns"] = ordered
    TABLE.parent.mkdir(parents=True, exist_ok=True)
    TABLE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    filled = sum(1 for column in ordered if column)
    print(f"{TABLE.relative_to(ROOT)} へ保存しました（{filled}/6列）")
    for index, column in enumerate(ordered, 1):
        print(f"  {index}列目: {'、'.join(column) or '(未入力)'}")
    if filled < 6:
        print("\n残りの列も会話で読み上げてから、もう一度実行してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
