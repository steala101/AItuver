# Remote LauncherのWindows完全非表示起動

- Date: 2026-07-31 21:48 JST
- Agent: Codex
- Status: completed

## 症状

Neuro Remote Launcher起動時、黒いコマンドプロンプトが常駐中ずっと残る。

## 原因

`RemoteLauncher.bat`は`pythonw.exe`が見つからない場合に`python.exe`へフォールバックしていた。
Remote Launcherは常駐プロセスなので、そのconsoleも終了せず残った。既存のWindows Startup
shortcutもbatchを直接targetにしていた。

## 変更

- `RemoteLauncher.vbs`を追加し、`WScript.Shell.Run command, 0, False`で完全非表示起動。
- venvに`pythonw.exe`がなくても、venvの`python.exe`をwindow style 0で起動する。
- `RemoteLauncher.bat`はPythonを直接起動せず、VBScriptへ即時委譲する互換入口に変更。
- `InstallAutoStart.bat`は`wscript.exe //B //Nologo`をtargetにするshortcutを作る。
- 実機の既存`NeuroRemoteLauncher.lnk`も同じtarget/argumentsへ更新し、再読確認した。
- `RemoteLauncher_Console.bat`は意図的な診断入口として変更していない。

## テスト

- focused: `21 passed in 0.13s`
- full: `1151 passed, 2 skipped in 44.72s`
- `py_compile`: launcher関連Pythonで成功
- skip 2件は既存の`pyflakes`未導入

## 受入確認

1. 現在の旧Launcherと黒いconsoleを終了する。
2. `RemoteLauncher.vbs`をダブルクリックする。
3. consoleが開かず、通知領域へLauncher iconが現れることを確認する。
4. 次回ログオンでも更新済みStartup shortcutからconsoleなしで起動することを確認する。
5. 問題調査時だけ`RemoteLauncher_Console.bat`を使い、consoleへログが出ることを確認する。

## Rollback

`RemoteLauncher.bat`の旧Python選択処理とInstallAutoStartのbatch targetへ戻し、
`RemoteLauncher.vbs`を削除する。ただし黒いconsole常駐問題が再発する。

## Related

- D-035
