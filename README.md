# agy-usage

`agy-usage` shows your Antigravity quota from the terminal: how much of each model group's 5-hour and weekly limits you have used, and when they reset.

```sh
uv tool install agy-usage
agy-usage
```

`agy-usage` is developed in [clanker-usage](https://github.com/wakamex/clanker-usage), which also has the documentation, the issue tracker, and matching monitors for Claude Code, Codex, Gemini CLI, Cursor, Devin, DeepSeek, OpenCode Go and Z.ai. This repository holds its history up to version 0.1.10 and is read-only.

Every version stays on PyPI. 0.4.0 changed the options, JSON output and cache location ([release notes](https://github.com/wakamex/clanker-usage/releases/tag/v0.4.0)); to keep the earlier behavior, install `agy-usage==0.1.10`.
