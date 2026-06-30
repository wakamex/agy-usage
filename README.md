# agy-usage

Antigravity CLI usage and quota monitor. It mirrors the small `ccusage`,
`gemini-cli-usage`, and `codex-cli-usage` tools: dependency-free Python,
terminal output, JSON output, statusline output, and a cache-refresh daemon.

## Example output

```text
Project: agy-usage
Model: Gemini 3.5 Flash (High)
  gemini-3.5-flash-high  12.4% used  resets 1h05m
History: 24 entries, latest 2026-06-29T22:53:01+00:00
```

Statusline:

```text
q:12.4% reset:1h05m model:Gemini_3.5_Flash_(High)
```

## Install

```bash
uv tool install agy-usage
```

For local development from a checkout:

```bash
uv tool install .
```

## Commands

| Command | Description |
|---------|-------------|
| `agy-usage` | Show current usage |
| `agy-usage status` | Same as above |
| `agy-usage json` | Print raw JSON |
| `agy-usage statusline` | Compact statusline output |
| `agy-usage refresh` | Force a fresh fetch, rewrite cache, and print status |
| `agy-usage daemon [-i SECS]` | Keep the cache fresh in the foreground |
| `agy-usage install` | Print setup instructions |

## Data sources

- Antigravity settings: `~/.gemini/antigravity-cli/settings.json`
- Antigravity OAuth token: `~/.gemini/antigravity-cli/antigravity-oauth-token`
- Antigravity command history: `~/.gemini/antigravity-cli/history.jsonl`
- Cache written by this tool: `~/.gemini/antigravity-cli/usage-limits.json`

Quota lookup uses the same Code Assist quota flow Antigravity logs mention:
`loadCodeAssist` followed by `retrieveUserQuota`.

## Options

```text
usage: agy-usage [-h] [--root ROOT] [-i INTERVAL] [--max-age MAX_AGE]
                 [--refresh]
                 {status,json,daemon,statusline,refresh,install}
```

- `--root ROOT`: inspect a different project root instead of the current directory
- `--max-age MAX_AGE`: cache TTL for `statusline`
- `--refresh`: ignore the cache and rebuild fresh data where applicable

Environment overrides:

- `AGY_USAGE_FILE`: alternate cache path
- `AGY_ACCESS_TOKEN`: provide an access token instead of reading Antigravity state
- `AGY_CODE_ASSIST_BASE_URL`: alternate Code Assist base URL
