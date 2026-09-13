# SBV2発音安定化 引継ぎ

## 症状

Style-Bert-VITS2の発音が以前より不自然に聞こえる。再生欠落やバッファunderflowではなく、短い読点の前後で抑揚が閉じて再開する挙動が主因候補だった。

## 調査結果

- `SentenceSegmenter`が読点も句点と同じ即時境界として扱い、8文字以上の短い節を別TTS requestへ確定していた。
- 現在使われているtsukuyomiモデルのstyleは`Neutral`一種類のみだった。
- 単一styleでもTemporalSelf由来のintonationをstyle weightへ掛けていたため、同じ話者の発音強度が会話状態に応じて揺れていた。
- 設定内の読み辞書に、発音訂正とは無関係な誤学習entryが一件残っていた。
- モデルassetの更新、再生underflow、SBV2 server errorを主因とする証拠は見つからなかった。

## 変更

- `neuro_voice/utils/textseg.py`
  - 句点・疑問符・感嘆符・改行はhard boundaryのまま維持。
  - 読点は20文字以上でのみflushするsoft boundaryへ変更。
  - 最大30文字のhard ceilingは維持し、低遅延と割り込み追跡の上限を変えていない。
- `neuro_voice/tts/style_bert_vits2.py`
  - `lock_single_style_weight`を追加。
  - styleが一種類のモデルはstyle weightを基準値へ固定。
  - 合成ログへ`speed`、`length`、`single_style`を追加。
- `neuro_voice/tts/factory.py` / `config/config.yaml`
  - 上記設定を既定ONで配線。
  - 無関係な誤学習読み辞書entryを削除。
- `tests/test_interaction.py` / `tests/test_style_bert_vits2.py`
  - 短い読点は次節を待つ、長い読点はflushする、単一styleのweight固定、設定OFF時の従来動作を追加。

## 検証状態

自動テストの実行を試みたが、Codex実行環境の利用上限により承認段階で拒否され、テストprocessは起動していない。コード不合格による失敗ではないが、passedとは扱わないこと。

## 実機受入

1. GUIを再起動して設定とTTS instanceを再生成する。
2. 読点を含む文章を複数回読み、読点前後で語尾が不自然に閉じないか確認する。
3. `logs/neuro_voice.log`でtsukuyomi合成が`single_style=True`、`w=1.00`になっているか確認する。
4. amitaro等の複数styleモデルへ切り替え、感情styleとweight変化が失われていないことを確認する。
5. Python実行が可能になったら、以下を実行する。
   - `.venv\\Scripts\\python.exe -m pytest tests/test_style_bert_vits2.py tests/test_interaction.py tests/test_reply_echo.py tests/test_delivery.py -q`

## 関連

- Decision: D-036
- Risk: R-044
- Changelog: 2026-07-31 22:02 JST

