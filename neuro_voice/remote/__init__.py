"""遠隔起動・Discord接続管理 (Remote Launcher)。

常駐する軽量ランチャーと、ランチャーが起動するAI本体 (--discordモード) の
間をつなぐ内部制御、状態機械、監査ログをまとめたパッケージ。

- state.py        … App/Discordの状態機械と排他遷移 (純粋ロジック)
- app_control.py  … AI本体側のlocalhost限定制御サーバー (Ready/join/leave等)
- launcher.py     … 常駐ランチャー本体 (起動/監視/終了/遠隔API)
"""
