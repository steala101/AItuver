# AI作業Handoff

- 担当AI: Claude
- Role: implementation
- Task: ゲームプロファイルを会話で切り替えられるようにする
- Started At: 2026-07-30 00:00 JST
- Finished At: 2026-07-30 00:30 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Work Lock: 単独編集

## 目的

`GameProfileSessionManager.handle_final_input` は冒頭で
「選択中がKTANEでなければ何もしない」と戻っていた。つまり Minecraft のまま
「爆弾解除を始めよう」と言っても**何も起きなかった**。
モードを変えるためだけに `config.yaml` を手で編集させる作りは、
音声で話しかける相手として不自然である。

（GPTによる先行解析の指摘を受けた作業。指摘は正しく、既存構造のままで実装できた）

## 調査結果

- 既存の土台がそろっていた:
  - `GameProfileRegistry` にゲーム名の別名解決（`ktane` / `爆弾解除` / `マイクラ` …）
  - `Config.set` / `Config.persist`（実行時の変更と `config.yaml` への書き戻し）
  - `selected` は呼ばれるたびに設定値を読み直すので**再起動なしで効く**
  - `pipeline.refresh_video_observation()` が `profile_allows_vision` を見て
    キャプチャを停止・再構築する
- state owner: プロファイルの選択は `config.video.game_profile`、
  セッションの生死は `GameProfileSessionManager`（第5条）
- Local/Discord parity: Discordの画面共有は**開始時に config を読む**ため、
  設定へ書き戻すだけで両方に効く。ローカルだけ、すでに走っている
  キャプチャを付け替える必要があるので `refresh_video_observation()` を呼ぶ

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/games/profiles.py` | `alias_items()`（長い別名が先）/ `labels()` | 「keep talking」で途中一致して終わらないため。`labels()` は「今使えるのは〜」用 |
| `neuro_voice/games/session.py` | `_SWITCH` / `_GAME_WORD` / `_WHICH` の3つの正規表現 | 下記「線引き」を参照 |
| `neuro_voice/games/session.py` | `switch_to()` — `persist` して、進行中セッションを終了 | 別のゲームへ移ったのに爆弾解除が生き残ると、以後の発話が持ち主のいない状態へ入る（第5条） |
| `neuro_voice/games/session.py` | `_switch_outcome()` / `_start()` を切り出し、`handle_final_input` の冒頭でKTANE以外を弾くのをやめた | 本題 |
| `neuro_voice/pipeline.py` | `_apply_switched_game_profile()` を追加し、`reason` が `game_profile_switched` で始まる時に呼ぶ | 設定値だけ変えても、起動時に始まったMinecraftのキャプチャは動いたまま残る |
| `tests/test_game_profile_switch.py`（新規25件） | **半分は誤爆しないことのテスト** | 下記 |
| `tests/test_game_profiles.py` | 旧挙動を固定していたテストを新しい意図へ書き換え、`FakeConfig` に `set`/`persist` を追加 | |
| `game_profiles/README.md` | 会話での切り替え方を追記 | |

## 線引き（この実装で一番大事なところ）

**切り替わるのは、「切り替えて」と読める言い方と、プロファイルの別名が
同じ発話に両方そろった時だけ。**

- 「静かにして」— `にして` は当たるが別名がない → **何もしない**
- 「マイクラの話なんだけどさ」— 別名はあるが切り替えの意図がない → **何もしない**
- 「爆弾解除って難しそうだよね」— 同上 → **何もしない**
- 「スプラトゥーンモードに切り替えて」— 切り替えの意図 + `モード` はあるが
  別名が無い → **切り替えずに、使えるものを答える**

「切り替えの語だけあって別名が無い」場合に聞き返すのは、`ゲーム|モード|
プロファイル` が同じ文にある時に限る。そうしないと「静かにして」で
使えるゲームの一覧を読み上げ始める。

## 使い方

```
爆弾解除モードにして        → KTANEへ切り替え
爆弾解除を始めよう          → KTANEへ切り替えて、そのままセッション開始
マイクラに戻して            → Minecraftへ戻す（爆弾解除セッションは終了）
今どのゲームモード？        → 現在のプロファイルとセッションの進行状況
```

## 変更しなかったもの

- 返答の文面はコード側の固定文のまま。既存の `_START` / `_STOP` と同じ扱い。
  役割の説明は `grounded_context()` が担っており、そちらはLLMが読む
- Discordの画面共有まわり。開始時に config を読むので、共有の設定変更で足りる

## 設計判断

- **状態の変更なので、どの発話がそれに当たるかはコードが決める**（第5条）。
  ただし線引きは狭く取り、迷う発話は普通の会話として通す
- **存在しないプロファイルへは絶対に移さない。** 名前が引けなければ、
  使えるものを答えて終わる
- **映像更新の失敗で会話を止めない**（第17条）。切り替えの返事はもう返っており、
  映像が付かないことは会話が止まる理由にならない

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/test_game_profile_switch.py -q` | 25 passed | サンドボックス |
| 全体回帰（`--ignore` は既知の実機依存＋サンドボックスに無い `.vbs`） | **1114 passed / 17 subtests** | サンドボックス |
| `pytest tests/test_no_undefined_names.py` | 3 passed（pyflakes） | サンドボックス |

- 未実施: 実機での音声入力による切り替え（テキスト入力経路では自動テスト済み）
- Manual: なし

## 設定・Migration

- Config: `video.game_profile` が**会話で書き換わるようになった**。
  ファイルはコメント・構造を保ったまま該当行だけ更新される
- DB schema / Data migration: なし

## 失敗した試行

なし。

## 残課題

- 別のゲームを足す時は `game_profiles/<id>/profile.json` を置き、`aliases` に
  呼び名を並べる。切り替えのコードは触らなくてよい
- 実機で音声から言った時に、STTの揺れ（「爆弾解除」→「爆断解除」等）で
  別名に当たらない可能性がある。`aliases` に読みの揺れを足すのが対処

## Rollback

`session.py` の `_switch_outcome` の呼び出しを外し、`handle_final_input` の
冒頭にKTANE判定を戻せばよい。`pipeline.py` の1箇所も併せて外す。

## 憲法・ADRへの影響

- Constitution amendment required: なし
- ADR added/updated: なし
- Architecture/Codemap updated: 未実施

## 秘密・個人情報確認

- `.env`値を記録していない: はい
- raw audio/imageを添付していない: はい
- private transcriptを複製していない: はい

## 追記（2026-07-30 01:10）— 切り替えただけでは版も答えられなかった

実機で「爆弾解除モードにして」のあと版と検証コードを聞いたら答えられなかった。

- 原因: `grounded_context()` が **`manual_expert` かつセッション未開始なら
  `None` を返していた**。ポッポの手元に情報がゼロで、しかもKTANE選択中は
  ウェブ検索も止まる（`mind.dialogue_allows_tool`）ので答えようがない
- 「始めよう」と言うまで版すら答えられない、というのは**利用者から見えない
  前提条件**だった。会話で切り替えられるようにした以上、なおさら不自然
- 未開始の時は `None` ではなく**短い参照だけ**返すようにした（約90tok、
  KTANEが選ばれている時だけ）。手順の指示はセッション開始後のまま

**ついでに見つかった二重管理**: 版 `1-ja` と検証コード `122` が
`profile.json` / `manual/sources.json` / **`session.py` のリテラル**の
3箇所にあった。`profile.json` を直しても発話は古い値を言い続ける状態。
`GameProfile.manual_version` / `manual_verification_code` が正本を読み、
`manual_reference` が文面を作る形にした。値が無ければ空文字を返すので、
マニュアルを持たないプロファイルで中身の無い参照文は作らない。

テスト +4件（開始前に版が答えられる／未開始と明示される／`profile.json` を
直せば発話も変わる／マニュアルが無ければ言及しない）。全体 **1118 passed**。

## 次の担当への一文

**会話品質の調査（`tools/check_sampling.py` の実行）とは独立した変更。
そちらの優先順位は動いていない。**
