# Handoff: folder-backed game profiles and KTaNE Expert

- Date: 2026-07-26 17:00 JST
- Agent: Codex
- Status: implementation complete; device-level voice play not run

## Goal

ゲームプロファイルが増えても一覧性を失わないフォルダ構成を作り、Keep Talking and
Nobody Explodesではポッポをゲーム操作側ではなくマニュアル担当Expertとして動かす。

## Implemented

- `game_profiles/<game_id>/profile.json`を正本にした。
- Minecraftを新構成へ登録し、従来の`video.game_profile: minecraft`を維持した。
- KTaNEは日本語公式マニュアル`1-ja`、検証コード`122`を固定し、公式URLをsourcesへ記録した。
- `GameProfileRegistry`はID/folder一致、重複、不正IDを拒否し、GUI用summaryを返す。
- `GameProfileSessionManager`はpersona別に状態を保存し、Local/Discordで同じ開始・終了応答を返す。
- Expert session中はWeb検索を拒否し、映像サービスを止め、見えない情報を推測しないpromptを注入する。
- 設定画面のプロファイル選択肢をフォルダから動的生成する。

## Verification

- bundled Pythonで変更Python全体の`compileall`成功。
- `GameProfileTests` 5件と既存`TurnClosureTests` 9件、計14件成功。

## Remaining / risk

- 全モジュールの判断表をローカルcode-owned solverにしていない。現段階ではprofile、
  official manual version、役割、安全制約の基盤まで。
- 実機で設定からKTaNEを選択し、「爆弾解除を始めよう」をLocal/Discord双方で確認する。
- KTaNE選択時にOBSが停止し、Minecraftへ戻すと従来映像が再開できることを確認する。

## Rollback

`video.game_profile`を`minecraft`または空へ戻す。新規session JSONは削除不要で、選択profileが
一致しない限りactiveにならない。
