# Discord DAVE receiver sidecar

This directory is managed by the Python application.  It runs only on
`127.0.0.1`, receives/decrypts Discord DAVE voice with Dysnomia, decodes Opus
to PCM, and sends that PCM to Python over authenticated local WebSocket IPC.

Do not start it manually during normal use.  Select **DAVE直接受信** in the
Discord settings, then join a voice channel from the app.  The Python process
passes the bot token only through the child process environment; neither the
sidecar nor the IPC protocol writes it to a project file or log.

The `source_account` metadata is the Discord account which sent an RTP stream.
It is intentionally not a voiceprint/profile identity.  Python performs
voiceprint recognition independently and does not automatically bind either
identity to the other.
