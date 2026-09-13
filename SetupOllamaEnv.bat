@echo off
rem Set Ollama server env vars for GPU/VRAM optimization (user scope, persistent).
rem After running this, RESTART Ollama (quit tray icon and relaunch) to apply.
rem These reduce VRAM (context length + KV cache q8_0) and keep one model / one job.
chcp 65001 >nul
title Setup Ollama Env (VRAM optimization)

echo Setting user environment variables for Ollama...
setx OLLAMA_CONTEXT_LENGTH 16384
setx OLLAMA_KV_CACHE_TYPE q8_0
setx OLLAMA_FLASH_ATTENTION 1
setx OLLAMA_NUM_PARALLEL 1
setx OLLAMA_MAX_LOADED_MODELS 1

echo.
echo [OK] Done. Current values:
echo   OLLAMA_CONTEXT_LENGTH = 16384  (config の llm.num_ctx と必ず同じ値にする)
echo     8192では毎ターン最大24件の履歴が捨てられていた(systemだけで5000〜6000tok)。
echo     16384でもVRAMは +16MB しか増えない(実測)。KVキャッシュが q8_0 で量子化され、
echo     Flash Attention が有効なため。下の2行を消すと この前提が崩れる。
echo   OLLAMA_KV_CACHE_TYPE  = q8_0   (KVキャッシュを8bit量子化。VRAM節約)
echo   OLLAMA_FLASH_ATTENTION= 1      (Flash Attention有効化。速度/VRAM改善)
echo   OLLAMA_NUM_PARALLEL   = 1      (同時生成を1に。VRAM/速度安定)
echo   OLLAMA_MAX_LOADED_MODELS = 1   (同時ロードモデルを1に)
echo.
echo [!] 反映には Ollama の再起動が必要です:
echo     タスクトレイの Ollama を右クリック -> Quit  してから Ollama を起動し直してください。
echo     (KVキャッシュ量子化 q8_0 は Flash Attention 有効時のみ効きます)
echo.
echo 元に戻す場合は各変数を削除:  setx OLLAMA_KV_CACHE_TYPE ""  等、または システム環境変数から削除。
pause
