# Handoff: ポッポの無邪気さ・遊び心・軽い生意気さ

- Date: 2026-07-26 17:54 JST
- Agent: Codex
- Status: completed（実会話の好み調整は未実施）
- Scope: ポッポの会話人格、Planner、HumorEngine、Surface Realizer、設定、回帰テスト

## 目的

ポッポの子供っぽさを、幼児語や毎回の決まり文句ではなく、好奇心がすぐ表に出ること、
遊びへの乗りのよさ、小さな出来事を楽しむこと、時々の軽い生意気さとして実装する。
ネウロ様の発言を複製するのではなく、ポッポ固有の人格として育てる。

## 実装

- `working_memory.py`
  - `playfulness`、`cheekiness`、`spontaneity`を追加。
  - `PERSONA_TRAIT_LABELS`へ追加したため設定GUIのpersona会話軸へ自動表示される。
- `config/config.yaml`
  - ポッポだけ新3軸を高めに設定。
  - 幼児語・固定語尾を避け、文脈由来の冗談と場面別抑制をsystem promptへ記載。
- `conversation_planner.py`
  - 新3軸をplayful、energetic、opinionated、playful_twist、HumorEngine、ValueSystemの重みへ反映。
  - repeat penaltyは維持し、高値でも毎回同じスタイルにならない。
- `feature_engines.py`
  - `mini_drama`、`playful_challenge`、`mock_grandiosity`を追加。
  - persona traitsを参照しつつ、support/correction/深刻語/低気分では従来どおりHumorEngineを停止。
- `surface_realizer.py`
  - 子供っぽさを無知の演技でなく、素直な反応と遊び心として表現。
  - 生意気さはcomfort 0.45以上でのみ相手へ向けられ、それ未満では自分・状況へ向ける。
  - 一度の発話ですべてを出さず、最も自然な一つだけを選ぶ。
- `intelligence.py`
  - persona traitsをfeature contextへ渡す。
  - 「遊び心」「生意気」「無邪気」「リアクション」等への明示評価を微小driftへ反映。

## 検証

使用環境:
`C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe`

成功:

- `python -m unittest tests.test_conversation_generation tests.test_conversation_features`
  - 25 tests
- `python -m unittest tests.test_interaction`
  - 139 tests
- `python -m compileall -q neuro_voice`

合計164 tests成功。

## 実機受入

GUI再起動後、次を分けて試す。

1. 成功・面白い出来事: 短い無邪気な反応や一度だけの遊びが出るか。
2. 軽い雑談: たまに張り合う・得意げになる反応が出るか。毎回ではないか。
3. 訂正: 茶化さず、まず訂正を受け取るか。
4. 落ち込み・真剣な相談: 冗談と生意気さが引っ込むか。
5. 10〜20ターン: 同じ小ボケ・語尾・自称を繰り返さないか。

強さの調整は`persona.presets.neuro.conversation_traits`の
`playfulness`、`cheekiness`、`spontaneity`を先に変更する。
global既定や安全gateは他personaへ影響するため、最初の調整対象にしない。

## Rollback

- ポッポ固有の見た目だけ戻す場合は、`config/config.yaml`のポッポ3軸とpersona文だけを戻す。
- 機構ごと無効化する場合は、新3軸を0.5へ戻し、Planner/Surface/Engineの参照を外す。
- DB migrationはない。既存persona dataは新軸がなくても既定値で動く。
