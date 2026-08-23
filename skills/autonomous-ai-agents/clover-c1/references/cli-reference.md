# Clover CLI Reference

Live sources when anything looks stale: `clover --help`, `clover <command> --help`,
https://clover-c1.local/docs/reference/cli-commands

### Global Flags

```
clover [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
clover chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
clover setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
clover model                Interactive model/provider picker
clover fallback [add|remove|list]  Fallback provider chain
clover config [show|edit|get|set|unset|path|env-path|check|migrate]
clover login / logout       OAuth sign-in / clear stored auth
clover doctor [--fix]       Check dependencies and config
clover status [--all]       Component status
```

### Tools & Skills

```
clover tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

clover skills list|browse|search QUERY|inspect ID
clover skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
clover skills config        Enable/disable skills per platform
clover skills check|update|uninstall|publish PATH
clover skills tap add REPO  Add a GitHub repo as a skill source
clover bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
clover mcp add NAME (--url or --command) | remove | list | test NAME
clover mcp catalog | install NAME     Curated catalog install
clover mcp configure NAME             Toggle tool selection
clover mcp serve                      Run Clover as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
clover gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `clover photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://clover-c1.local/docs/user-guide/messaging/

### Sessions

```
clover sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
clover cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
clover webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
clover profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
clover profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
clover auth                 Interactive credential manager
clover auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
clover auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
clover desktop / gui        Native desktop app
clover dashboard            Web admin panel + embedded chat (--stop / --status)
clover proxy                OpenAI-compatible local proxy backed by an OAuth provider
clover portal               Quick setup / sign in via Clover Portal
clover kanban <verb>        Multi-agent work-queue board
clover project              Named multi-folder workspaces
clover skin list|use|set    Switch/tweak skins (see references/themes.md)
clover pets <verb>          Pet mascots (see references/petdex.md)
clover memory setup|status|off|reset   Memory provider
clover secrets bitwarden|onepassword   External secret stores
clover moa                  Mixture-of-Agents slots
clover hooks / security / backup / import / checkpoints / console
clover logs [-f] [errors]   View agent/error logs
clover send                 One-off message through a gateway platform
clover pairing / plugins / insights / journey / computer-use
clover acp                  ACP server (IDE integration)
clover completion bash|zsh|fish
clover update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `clover photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `clover config edit` · [Configuration docs](https://clover-c1.local/docs/user-guide/configuration) |
| Tools / toolsets | `clover tools list` · [Tools reference](https://clover-c1.local/docs/reference/tools-reference) |
| Skills catalog | `clover skills browse` · [Skills catalog](https://clover-c1.local/docs/reference/skills-catalog) |
| Provider setup | `clover model` · [Providers guide](https://clover-c1.local/docs/integrations/providers) |
| Env variables | `clover config env-path` · [Env vars reference](https://clover-c1.local/docs/reference/environment-variables) |
| Gateway logs | `~/.clover/logs/gateway.log` (or `clover logs`) |
| Sessions | `clover sessions browse` (reads state.db) |
