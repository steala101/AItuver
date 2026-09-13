# 重複応答の誤検知分離と自然な一回再生成

- Date: 2026-07-30 23:20 JST
- Agent: Codex
- Status: completed

## 依頼と観測

会話中に重複候補が頻繁に検出され、固定の診断謝罪が読み上げられるとの報告を受けた。
最新ログを調べると、直前assistant本文をほぼ再生成した真の重複と、短い聞き返し・
定型導入が数turn前の文面に部分一致した誤検知が混在していた。

## 原因

1. `ReplyEchoGuard`へ直近assistant 4件と現在user発話を区別せず渡していた。
2. 実質8文字から各文を検査したため、短い定型導入だけでも古い発話へ高くcontainmentした。
3. 自己反復とuserオウム返しが同じ閾値だった。
4. 抑止後は会話の必要性に関係なく固定診断文をTTSへ渡していた。

## 実装

- `EchoSource(kind=assistant|user)`と`EchoMatch(kind, score)`を追加。
- assistantは既存の意味的containment、userは0.96以上かつ16実質文字以上で判定。
- probeの最小長を`probe_min_chars`へ統一し、短い導入だけで即時repeatにしない。
- Local/Discordともassistant証拠を直近2件へ限定し、同一の最終assistant文は重複登録しない。
- 抑止event/logへ`repeat_assistant`または`repeat_user`とscoreを記録する。
- stream中にrepeat確定したguardは即時閉じ、post-stream flushで同じ候補を二重判定しない。
- 通常重複では固定謝罪を廃止。同じturn内で一回だけ新内容を再生成し、`<NO_REPLY>`なら
  自然に無言で閉じる。再生成も重複なら二度目を発話しない。
- 明示訂正は従来どおりcodeで訂正内容を短く確定する。

## テスト

- `py_compile`: 変更した4 Python moduleで成功。
- focused: `46 passed in 0.41s`。
- full: 最終状態で`1148 passed, 2 skipped in 55.96s`。
- skip 2件は既存の`pyflakes`未導入。

## 実機で見るログ

- `重複候補を発話前に抑止: kind=assistant|user score=...`
- `reply_regenerated` eventの`success`
- 再生成も重複した場合のwarning

会話本文はprivacy方針により本handoffへ転記していない。

## 受入確認

1. 直前返答へ内容のない口語同意を返し、同文も固定診断謝罪も読まないこと。
2. 前turnへ新しい事実を足す返答では、短い定型導入が同じでも誤抑止しないこと。
3. 短いASR誤認へ聞き返せること。
4. 質問に対してモデルが直前本文を再生成した場合、一回だけ新しい回答へ復旧すること。
5. Discord 1対1とgroupでも同じ挙動で、`NO_REPLY`時に空TTSを作らないこと。

## Remaining

- 重複が起きたturnだけ追加LLM一回分の遅延がある。
- 三文目以降から始まる意味的反復は低遅延優先で対象外。
- 実GUI/Discordでkind別発生率と再生成成功率を確認し、assistant閾値または証拠数を
  変更する場合は匿名fixtureを先に追加する。

## Rollback

`echo.py`のsource型・probe条件、両surfaceの一回再生成block、`repair.py`のretry helperを
まとめて戻す。固定診断謝罪だけを単独復活させない。

## Related

- ADR-0002
- D-034
- R-043
