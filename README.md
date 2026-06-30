# agy-usage

Antigravity CLI usage and quota monitor. It mirrors the small `ccusage`,
`gemini-cli-usage`, and `codex-cli-usage` tools: dependency-free Python,
terminal output, JSON output, statusline output, and a cache-refresh daemon.

## Example output

```text
Project: agy-usage
Model: Gemini 3.5 Flash (High)

GEMINI MODELS
  Models within this group: Gemini Flash, Gemini Pro
  Weekly Limit    95.0% remaining  resets 6d19h
  Five Hour Limit 62.5% remaining  resets 7m

CLAUDE AND GPT MODELS
  Models within this group: Claude Opus, Claude Sonnet, GPT-OSS
  Weekly Limit    100.0% remaining  quota available
  Five Hour Limit 100.0% remaining  quota available
History: 24 entries, latest 2026-06-29T22:53:01+00:00
```

Statusline:

```text
q:62.5%left reset:7m model:Gemini_3.5_Flash_(High)
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

Quota lookup mirrors the Antigravity CLI's own backend calls:

1. `loadCodeAssist` with `{"metadata":{"ideType":"ANTIGRAVITY"}}`
2. `retrieveUserQuotaSummary` using the returned `cloudaicompanionProject`

Expired Antigravity OAuth access tokens are refreshed with the stored refresh
token and the same Google OAuth client metadata used by the CLI, then written
back to `antigravity-oauth-token`.

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
