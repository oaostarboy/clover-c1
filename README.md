<p align="center">
  <img src="assets/banner.png" alt="Clover Cognition" width="100%">
</p>

# Clover Cognition 🍀
<p align="center">
  <a href="#">Clover Cognition</a> | <a href="#">Clover Desktop</a>
</p>
<p align="center">
  <a href="docs/"><img src="https://img.shields.io/badge/Docs-clover--c1-FFD700?style=for-the-badge" alt="Documentation"></a>
  <a href="#"><img src="https://img.shields.io/badge/Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" alt="Discord"></a>
  <a href="#"><img src="https://img.shields.io/badge/License-MIT-green?style=for-the-badge" alt="License: MIT"></a>
  <a href="#"><img src="https://img.shields.io/badge/Built%20by-Anthony%20Nguyen-blueviolet?style=for-the-badge" alt="Built by Anthony Nguyen"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Lang-中文-red?style=for-the-badge" alt="中文"></a>
  <a href="README.ur-pk.md"><img src="https://img.shields.io/badge/Lang-اردو-green?style=for-the-badge" alt="اردو"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-orange?style=for-the-badge" alt="Español"></a>
</p>

A personal AI agent that gets better at working with you, on purpose.

Clover runs in your terminal, on a $5 VPS, or in a sandbox that costs almost nothing while it sleeps. It has 92 tools, a real TUI, and a messaging gateway, so you can start something on your laptop and check on it from Telegram at the bus stop.

The clover is on the label. Underneath it is bookkeeping. After every turn, Clover asks itself whether it learned anything worth keeping, and writes it down. Do that a few hundred times and the agent knows your projects, your conventions, and the three commands you always forget. That looks like luck from the outside.

---

## The part under the label

Four mechanisms, all in the tree, all inspectable.

**It reviews its own work.** After a turn finishes, `agent/background_review.py` forks the agent onto a daemon thread, replays the conversation, and asks one question: should any of this be saved as a memory or a skill? The fork runs with a tool whitelist limited to memory and skill management, so it can write notes and nothing else. Your conversation and your prompt cache are never touched.

**It writes its own skills.** A skill is procedural memory: how to do one specific kind of task, captured from a time it worked. The agent creates, edits, patches, and deletes them through the `skill_manage` tool, and new ones land in `~/.clover/skills/`. The format is the open [agentskills.io](https://agentskills.io) standard, so skills written for other agents drop straight in, and yours travel back out.

**It can search its own past.** `session_search` runs FTS5 over every conversation you have had, dedupes hits by session lineage, and hydrates the top result with a window of surrounding messages. It can then scroll through that session like a file. Discovery costs zero tokens of inference, so "what did we decide about the migration in June" is a cheap question.

**It builds a model of you.** Memory backends are pluggable: Honcho, mem0, supermemory, byterover, hindsight, holographic, openviking, and retaindb ship in `plugins/memory/`. Bring your own by implementing one ABC and dropping it in `~/.clover/plugins/`.

None of that is luck. It is a filing habit with a good UI.

---

## Install

### Linux, macOS, WSL2, Termux

```bash
curl -fsSL  | bash
```

### Windows (native, PowerShell)

```powershell
iex (irm )
```

Native Windows is fully supported without WSL. CLI, gateway, TUI, and tools all run natively. The installer brings uv, Python 3.11, Node.js, ripgrep, ffmpeg, and a portable Git Bash (MinGit, unpacked to `%LOCALAPPDATA%\clover\git`, no admin required). Clover uses that bundled Git Bash to run shell commands, and it stays isolated from any system Git. If you already have Git, the installer finds it and uses that instead. Otherwise the MinGit download is about 45MB.

Native Windows installs live under `%LOCALAPPDATA%\clover`. WSL2 installs under `~/.clover`, same as Linux. The Linux one-liner above works fine in WSL2 if you prefer it.

**Android / Termux:** follow the [Termux guide](docs/getting-started/termux). Termux gets a curated `.[termux]` extra, because the full `.[all]` extra currently pulls voice dependencies that Android cannot build.

Then:

```bash
source ~/.bashrc    # or ~/.zshrc
clover
```

<details>
<summary><b>Troubleshooting: antivirus flags <code>uv.exe</code> on Windows</b></summary>

<br>

If Bitdefender, Windows Defender, or similar quarantines `uv.exe` from `%LOCALAPPDATA%\clover\bin\uv.exe`, it is a false positive. That file is Astral's `uv`, the Rust package manager Clover bundles to manage its Python environment. ML-based engines flag unsigned Rust binaries that download and install packages, which is a fair description of every package manager ever written.

Verify your copy is authentic:

```powershell
# Install GitHub CLI if needed
winget install --id GitHub.cli

# Login to GitHub
gh auth login

# Run verification
$uv = "$env:LOCALAPPDATA\clover\bin\uv.exe"
$ver = (& $uv --version).Split(' ')[1]
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$zip = "$env:TEMP\uv.zip"
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip -UseBasicParsing
gh attestation verify $zip --repo astral-sh/uv
Expand-Archive $zip "$env:TEMP\uv_x" -Force
(Get-FileHash "$env:TEMP\uv_x\uv.exe").Hash -eq (Get-FileHash $uv).Hash
```

If attestation says "Verification succeeded" and the last line prints `True`, you are good.

To whitelist:

- **Windows Defender:** PowerShell as Admin, then `Add-MpPreference -ExclusionPath "$env:LOCALAPPDATA\clover\bin"`
- **Bitdefender:** Protection > Antivirus > Settings > Manage Exceptions
- Whitelist the **folder**, not the file hash. Clover updates `uv` and the hash changes every version.

Upstream context: [astral-sh/uv#13553](https://github.com/astral-sh/uv/issues/13553), [astral-sh/uv#15011](https://github.com/astral-sh/uv/issues/15011), [astral-sh/uv#10079](https://github.com/astral-sh/uv/issues/10079).

</details>

---

## First five minutes

```bash
clover              # Interactive CLI, start a conversation
clover model        # Choose your provider and model
clover tools        # Configure which tools are enabled
clover config set   # Set individual config values
clover config get   # Print individual config values
clover gateway      # Start the messaging gateway
clover setup        # Full setup wizard, configures everything at once
clover claw migrate # Import an existing OpenClaw install
clover update       # Update to the latest version
clover doctor       # Diagnose issues
```

Start with `clover setup` if you want the guided path, or just run `clover` and change your mind later.

📖 **[Full documentation →](docs/)**

---

## Bring your own model

Clover talks to any OpenAI-compatible API. The provider registry in `clover_cli/auth.py` currently ships 37 entries, including Clover Portal, OpenRouter, OpenAI, Anthropic, Google AI Studio, xAI, DeepSeek, Z.AI / GLM, Kimi / Moonshot, MiniMax, Qwen, NVIDIA NIM, Vercel AI Gateway, GitHub Copilot, LM Studio, and your own endpoint. Several support OAuth, so you can sign in with an existing subscription instead of minting a key.

Switch with `clover model`. No code changes, no lock-in, no rewriting your config when you change your mind next week. The full list lives in the [provider docs](docs/integrations/providers).

For OpenRouter users, `provider_routing` in `config.yaml` controls upstream selection: sort by throughput, latency, or price, allow or ignore specific providers, and set data retention policy.

### Or skip the key collection entirely

Clover Portal is one subscription that covers the parts you would otherwise assemble from five separate accounts:

- **300+ models**, selectable with `/model <name>`
- **Tool Gateway**: web search (Firecrawl), image generation (FAL), text-to-speech (OpenAI), and a cloud browser (Browser Use), all routed through the same subscription

```bash
clover setup --portal
```

That logs you in over OAuth, sets Clover as your provider, and turns on the Tool Gateway. Check what is wired up with `clover portal info`. The gateway is per-backend, so you can keep using your own key for any single tool whenever you want. Details on the [Tool Gateway docs page](docs/user-guide/features/tool-gateway).

---

## Where it runs

The terminal tool has seven backends: local, Docker, SSH, Singularity, Modal, Daytona, and Vercel Sandbox. Same tools, same agent, different machine.

Modal and Daytona are the interesting ones. Both offer serverless persistence, so the environment hibernates when idle and wakes on demand. An agent you talk to twice a week costs close to nothing between those two conversations.

---

## Where you talk to it

Run `clover gateway` and Clover shows up on Telegram, Discord, Slack, WhatsApp, Signal, iMessage, and email, from a single process. Around twenty more adapters ship in `plugins/platforms/`: Matrix, IRC, Teams, Google Chat, Feishu, DingTalk, LINE, Mattermost, SMS, ntfy, Home Assistant, and generic webhooks. Voice memos get transcribed. Conversations continue across platforms, so you can start in the terminal and finish on your phone.

Both entry points share most slash commands.

| Action | CLI | Messaging platforms |
| --- | --- | --- |
| Start chatting | `clover` | `clover gateway setup` + `clover gateway start`, then message the bot |
| Start fresh conversation | `/new` or `/reset` | `/new` or `/reset` |
| Change model | `/model [provider:model]` | `/model [provider:model]` |
| Set a personality | `/personality [name]` | `/personality [name]` |
| Retry or undo the last turn | `/retry`, `/undo` | `/retry`, `/undo` |
| Compress context, check usage | `/compress`, `/usage`, `/insights [--days N]` | `/compress`, `/usage`, `/insights [days]` |
| Browse skills | `/skills` or `/<skill-name>` | `/<skill-name>` |
| Interrupt current work | `Ctrl+C` or send a new message | `/stop` or send a new message |
| Platform-specific status | `/platforms` | `/status`, `/sethome` |

Full lists: [CLI guide](docs/user-guide/cli) and [Messaging Gateway guide](docs/user-guide/messaging).

---

## Everything else it does

**Programmatic tool calling.** `execute_code` lets the model write a Python script that calls Clover's tools over RPC, a Unix socket locally or file-based RPC on remote backends. Only the script's stdout comes back. A ten-step pipeline that would have burned ten turns and filled your context window becomes one turn and a few hundred tokens.

**Subagents.** `delegate_task` spawns isolated agents for parallel workstreams. They do the reading, you get the conclusion.

**Scheduled work.** A built-in cron scheduler runs jobs unattended and delivers results to any connected platform. Daily reports, nightly backups, weekly audits. Written in plain language, because the agent is the one reading them.

**MCP.** Connect any MCP server for extra capabilities, with OAuth handling included. Clover also runs as an MCP server itself via `mcp_serve.py`.

**A real terminal interface.** Multiline editing, slash-command autocomplete, conversation history, interrupt-and-redirect mid-task, streaming tool output, and a data-driven skin system if you want it a different color.

**Research tooling.** Batch trajectory generation and trajectory compression, for training the next generation of tool-calling models on what this one did.

---

## Documentation

| Section | What's covered |
| --- | --- |
| [Quickstart](docs/getting-started/quickstart) | Install, setup, first conversation, about two minutes |
| [CLI Usage](docs/user-guide/cli) | Commands, keybindings, personalities, sessions |
| [Configuration](docs/user-guide/configuration) | Config file, providers, models, all options |
| [Messaging Gateway](docs/user-guide/messaging) | Telegram, Discord, Slack, WhatsApp, Signal, Home Assistant |
| [Security](docs/user-guide/security) | Command approval, DM pairing, container isolation |
| [Tools & Toolsets](docs/user-guide/features/tools) | The tool catalog, toolset system, terminal backends |
| [Skills System](docs/user-guide/features/skills) | Procedural memory, Skills Hub, writing skills |
| [Memory](docs/user-guide/features/memory) | Persistent memory, user profiles, best practices |
| [MCP Integration](docs/user-guide/features/mcp) | Connecting MCP servers |
| [Cron Scheduling](docs/user-guide/features/cron) | Scheduled tasks with platform delivery |
| [Context Files](docs/user-guide/features/context-files) | Project context that shapes every conversation |
| [Architecture](docs/developer-guide/architecture) | Project structure, agent loop, key classes |
| [Contributing](docs/developer-guide/contributing) | Development setup, PR process, code style |
| [CLI Reference](docs/reference/cli-commands) | All commands and flags |
| [Environment Variables](docs/reference/environment-variables) | Complete env var reference |

---

## Coming from OpenClaw

`clover setup` detects `~/.openclaw` on first run and offers to migrate before configuration starts. You can also do it later:

```bash
clover claw migrate              # Interactive migration (full preset)
clover claw migrate --dry-run    # Preview what would move
clover claw migrate --preset user-data   # Everything except secrets
clover claw migrate --overwrite  # Overwrite existing conflicts
```

It imports your SOUL.md persona, MEMORY.md and USER.md entries, user-created skills (into `~/.clover/skills/openclaw-imports/`), your command allowlist, messaging platform config, allowlisted API keys (Telegram, OpenRouter, OpenAI, Anthropic, ElevenLabs), TTS assets, and AGENTS.md workspace instructions with `--workspace-target`.

Run `clover claw migrate --help` for the full set of options, or use the `openclaw-migration` skill if you would rather have the agent walk you through it with dry-run previews.

---

## Contributing

Contributions are welcome. The [Contributing Guide](CONTRIBUTING.md) covers development setup, architecture, code style, and what gets merged.

The short version: use the standard installer, then work from the git checkout it creates at `$CLOVER_HOME/clover-c1` (usually `~/.clover/clover-c1`). That is the layout `clover update`, the managed venv, lazy dependencies, the gateway, and the docs tooling all assume.

```bash
curl -fsSL  | bash
cd "${CLOVER_HOME:-$HOME/.clover}/clover-c1"
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

For a throwaway clone or a CI job where you deliberately do not want the managed layout, create the venv **outside** the source tree. A venv inside the directory the agent operates from can be deleted by a relative-path command the agent runs against its own checkout, which destroys the running runtime mid-session.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv ~/.clover/venvs/clover-dev --python 3.11
source ~/.clover/venvs/clover-dev/bin/activate
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

---

## Community

- 📚 [Skills Hub](https://agentskills.io)

---

## License

MIT, see [LICENSE](LICENSE).

Built by Anthony Nguyen.
