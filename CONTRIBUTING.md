# Contributing to Clover Cognition

This guide covers what you need to contribute: dev setup, the architecture, what we merge, and how to get a PR through review.

Read the sections about placement (skill vs tool, in-tree vs plugin) before you write code. Most closed PRs here are good code in the wrong place, and that is a frustrating way to find out.

---

## What we merge, in order

1. **Bug fixes.** Crashes, incorrect behavior, data loss. Always first.
2. **Cross-platform compatibility.** macOS, Linux distros, native Windows, WSL2. Clover should work everywhere.
3. **Security hardening.** Shell injection, prompt injection, path traversal, privilege escalation. See [Security](#security-considerations).
4. **Performance and robustness.** Retry logic, error handling, graceful degradation.
5. **New skills**, if they are broadly useful. See [Skill or tool?](#skill-or-tool)
6. **New tools**, rarely. Most capabilities should be skills. See below.
7. **Documentation.** Fixes, clarifications, examples.

---

## Search first

A minute of searching saves an afternoon of work. Duplicates are common here.

Search open **and** merged PRs and issues. The duplicate check in the PR template fires at review time, which is after you have already built the thing:

```bash
gh search issues --repo clover-c1 "<your terms>"
gh search prs --repo clover-c1 --state all "<your terms>"
```

The issue tracker lags the code. Plenty of requested features are already implemented in-tree, so grep the source before proposing a capability. If an open PR already covers it, review that one instead of opening a competitor. For anything large, comment on the issue so two people do not build it twice.

Related: #38284 covers the agent-side version of this, Clover checking existing issues and PRs before deep self-troubleshooting. This section is the human half.

---

## Skill or tool?

The most common question from new contributors, and the answer is almost always **skill**.

**Make it a skill when:**

- The capability is instructions plus shell commands plus tools that already exist
- It wraps an external CLI or API the agent can reach through `terminal` or `web_extract`
- It does not need custom Python integration or API key management inside the agent
- Examples: arXiv search, git workflows, Docker management, PDF processing, email through CLI tools

**Make it a tool when:**

- It needs end-to-end integration with API keys, auth flows, or multi-component config managed by the harness
- It needs logic that must execute precisely every time, not best-effort from LLM interpretation
- It handles binary data, streaming, or real-time events that cannot go through a terminal
- Examples: browser automation (Browserbase session management), TTS (audio encoding plus platform delivery), vision analysis (base64 image handling)

### Should the skill be bundled?

Bundled skills in `skills/` ship with every install, so they need to earn their place in everyone's context window. Document handling, web research, common dev workflows, system administration: things a wide range of people use regularly.

Official but not universal (a paid service integration, a heavyweight dependency) goes in **`optional-skills/`**. It ships with the repo, stays inactive by default, and users find it through `clover skills browse` (labeled "official") and install it with `clover skills install`. No third-party warning, because we vouch for it.

Specialized, community-contributed, or niche belongs on a **Skills Hub**. Upload it to a registry, share it in the Discord, and users install it with `clover skills install`.

---

## Memory providers ship as standalone plugins

**We are no longer accepting new memory providers into this repo.** The built-in set under `plugins/memory/` (honcho, mem0, supermemory, byterover, hindsight, holographic, openviking, retaindb) is closed. New memory backends should be **standalone plugin repos** that users install into `~/.clover/plugins/` or via a pip entry point.

Standalone memory plugins are not second-class. They:

- Implement the same `MemoryProvider` ABC (`agent/memory_provider.py`): `sync_turn`, `prefetch`, `shutdown`, and optionally `post_setup(clover_home, config)` for setup-wizard integration
- Use the same discovery system, since `discover_memory_providers()` picks them up from user and project plugin directories and pip entry points
- Integrate with `clover memory setup` through `post_setup()`, no core changes
- Register their own CLI subcommands via `register_cli(subparser)` in a `cli.py`
- Get the same lifecycle hooks and config plumbing as in-tree providers

PRs adding a new directory under `plugins/memory/` get closed with a pointer to publish it as its own repo. Existing in-tree providers stay, and bug fixes to them are welcome.

This is a coupling decision, not a quality bar. Memory providers are the most common plugin type and they should not all live in this tree.

---

## Third-party product integrations ship as standalone plugins

Same rule, wider scope: **any plugin that integrates someone else's product does not land in this repo.** Observability backends, vendor SaaS connectors, analytics dashboards, paid-service tie-ins.

The reason is maintenance load. Every external product absorbed into core becomes ours to keep working against a fast-moving codebase, for a backend we do not own and cannot control. Clover ships often and the core moves quickly. Coupling third-party products into it creates an open-ended burden on maintainers.

Publish it as a standalone plugin instead:

- Implement the relevant ABC and use the existing discovery path (`~/.clover/plugins/`, project `.clover/plugins/`, or a pip entry point). See [Build a Clover Plugin](docs/guides/build-a-clover-plugin).
- Register lifecycle hooks (`pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`, `on_session_start`, `on_session_end`), tools (`ctx.register_tool`), and CLI subcommands (`ctx.register_cli_command`) through the surface we already expose. No core changes needed.
- If your plugin needs something the framework does not expose, that is a feature request to **widen the generic plugin surface** with a new hook or `ctx` method. Never special-case your plugin in core.
- Promote it in the Discord `#plugins-skills-and-skins` channel.

A well-built integration can pass every automated check and still be closed for this reason. It is a placement decision, not a verdict on your code.

---

## Development setup

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| **Git** | With the `git-lfs` extension installed |
| **Python 3.11 to 3.13** | uv will install it if missing |
| **uv** | Fast Python package manager ([install](https://docs.astral.sh/uv/)) |
| **Node.js 20+** | Optional, needed for browser tools and the WhatsApp bridge (matches root `package.json` engines) |

### Use the standard installer

The best dev bootstrap is the same path users take. Run the installer, then work inside the repository it cloned. It creates the Clover venv, wires the `clover` command, stamps the install method for `clover update`, and clones the full git project into `$CLOVER_HOME/clover-c1` (usually `~/.clover/clover-c1`). That keeps your environment on the layout the CLI, updater, lazy dependency installer, gateway, and docs tooling all assume.

```bash
curl -fsSL  | bash
cd "${CLOVER_HOME:-$HOME/.clover}/clover-c1"

# Add dev/test extras on top of the standard install.
uv pip install -e ".[all,dev]"

# Optional: docs site + workspace dependencies.
npm install
```

Then branch and test from that checkout:

```bash
git checkout -b fix/description
scripts/run_tests.sh
```

### Manual clone fallback

Use this only when you deliberately do not want the managed layout, for example a throwaway clone in a container or CI job. If you install this way, run the `clover` entrypoint from this venv. Running the system `python3 -m clover_cli.main` can pick up unrelated system packages.

Create the venv **outside** the source tree. A venv that lives inside the directory the agent operates from can be wiped by a relative-path command the agent runs against its own checkout (`rm -rf venv`, `uv venv venv`), which silently destroys the running runtime mid-session. Keeping it outside means no relative path from the workspace resolves to it.

```bash
git clone
cd clover-c1

# Create venv with Python 3.11, OUTSIDE the source tree
uv venv ~/.clover/venvs/clover-dev --python 3.11
export VIRTUAL_ENV="$HOME/.clover/venvs/clover-dev"
export PATH="$VIRTUAL_ENV/bin:$PATH"

# Install with all extras (messaging, cron, CLI menus, dev tools)
uv pip install -e ".[all,dev]"

# Optional: workspace / docs dependencies
npm install
```

### Configure

```bash
mkdir -p ~/.clover/{cron,sessions,logs,memories,skills}
cp cli-config.yaml.example ~/.clover/config.yaml
touch ~/.clover/.env

# At minimum, one LLM provider key:
echo "OPENROUTER_API_KEY=***" >> ~/.clover/.env
```

### Run

```bash
# The standard installer already put `clover` on PATH.
clover doctor
clover chat -q "Hello"
```

With the manual clone fallback, run `./clover` from the checkout or symlink the venv:

```bash
mkdir -p ~/.local/bin
ln -sf "$(pwd)/venv/bin/clover" ~/.local/bin/clover
```

### Tests

```bash
# Preferred, matches CI: hermetic `env -i`, per-file subprocess isolation
# via run_tests_parallel.py, worker count auto-scaled. See AGENTS.md.
scripts/run_tests.sh

# Alternative (activate the venv first). Run the wrapper before you open a
# PR anyway, for parity with GitHub Actions.
pytest tests/ -v
```

---

## Project structure

```
clover-c1/
├── run_agent.py              # AIAgent class: core conversation loop, tool dispatch, session persistence
├── cli.py                    # CloverCLI class: interactive TUI, prompt_toolkit integration
├── model_tools.py            # Tool orchestration (thin layer over tools/registry.py)
├── toolsets.py               # Tool groupings and presets (clover-cli, clover-telegram, etc.)
├── clover_state.py           # SQLite session database with FTS5 full-text search, session titles
├── batch_runner.py           # Parallel batch processing for trajectory generation
│
├── agent/                    # Agent internals (extracted modules)
│   ├── prompt_builder.py         # System prompt assembly (identity, skills, context files, memory)
│   ├── background_review.py      # Post-turn forked review: saves memories and skills
│   ├── context_compressor.py     # Auto-summarization when approaching context limits
│   ├── auxiliary_client.py       # Resolves auxiliary OpenAI clients (summarization, vision)
│   ├── display.py                # KawaiiSpinner, tool progress formatting
│   ├── model_metadata.py         # Model context lengths, token estimation
│   └── trajectory.py             # Trajectory saving helpers
│
├── clover_cli/               # CLI command implementations
│   ├── main.py                   # Entry point, argument parsing, command dispatch
│   ├── config.py                 # Config management, migration, env var definitions
│   ├── setup.py                  # Interactive setup wizard
│   ├── auth.py                   # Provider registry, OAuth, Clover Portal
│   ├── models.py                 # OpenRouter model selection lists
│   ├── banner.py                 # Welcome banner, ASCII art
│   ├── commands.py               # Central slash command registry (CommandDef), autocomplete, gateway helpers
│   ├── callbacks.py              # Interactive callbacks (clarify, sudo, approval)
│   ├── doctor.py                 # Diagnostics
│   ├── skills_hub.py             # Skills Hub CLI + /skills slash command
│   └── skin_engine.py            # Skin/theme engine: data-driven CLI visual customization
│
├── tools/                    # Tool implementations (self-registering)
│   ├── registry.py               # Central tool registry (schemas, handlers, dispatch)
│   ├── approval.py               # Dangerous command detection + per-session approval
│   ├── terminal_tool.py          # Terminal orchestration (sudo, env lifecycle, backends)
│   ├── file_operations.py        # read_file, write_file, search, patch, etc.
│   ├── web_tools.py              # web_search, web_extract (Parallel/Firecrawl + Gemini summarization)
│   ├── vision_tools.py           # Image analysis via multimodal models
│   ├── delegate_tool.py          # Subagent spawning and parallel task execution
│   ├── code_execution_tool.py    # Sandboxed Python with RPC tool access
│   ├── session_search_tool.py    # Search past conversations with FTS5 + anchored windows
│   ├── cronjob_tools.py          # Scheduled task management
│   ├── skill_manager_tool.py     # Agent-managed skill creation and editing
│   └── environments/             # Terminal execution backends
│       ├── base.py                   # BaseEnvironment ABC
│       ├── local.py, docker.py, ssh.py, singularity.py, modal.py, daytona.py, vercel_sandbox.py
│
├── gateway/                  # Messaging gateway
│   ├── run.py                    # GatewayRunner: platform lifecycle, message routing, cron
│   ├── config.py                 # Platform configuration resolution
│   ├── session.py                # Session store, context prompts, reset policies
│   ├── platform_registry.py      # Lazy platform plugin discovery and loading
│   └── platforms/                # In-tree adapters (signal, bluebubbles, weixin, yuanbao, webhook)
│
├── plugins/
│   ├── platforms/            # Bundled platform plugins (telegram, discord, slack, whatsapp, ...)
│   └── memory/               # Built-in memory providers (closed set: see above)
├── scripts/                  # Installer and bridge scripts
│   ├── install.sh                # Linux/macOS installer
│   ├── install.ps1               # Windows PowerShell installer
│   └── whatsapp-bridge/          # Node.js WhatsApp bridge (Baileys)
│
├── skills/                   # Bundled skills (copied to ~/.clover/skills/ on install)
├── optional-skills/          # Official optional skills (discoverable via hub, not activated by default)
├── tests/                    # Test suite
├── web/                      # Documentation site
│
├── cli-config.yaml.example   # Example configuration (copied to ~/.clover/config.yaml)
└── AGENTS.md                 # Development guide for AI coding assistants
```

### User configuration in `~/.clover/`

| Path | Purpose |
|------|---------|
| `~/.clover/config.yaml` | Settings (model, terminal, toolsets, compression, etc.) |
| `~/.clover/.env` | API keys and secrets |
| `~/.clover/auth.json` | OAuth credentials (Clover Portal) |
| `~/.clover/skills/` | All active skills (bundled + hub-installed + agent-created) |
| `~/.clover/memories/` | Persistent memory (MEMORY.md, USER.md) |
| `~/.clover/state.db` | SQLite session database |
| `~/.clover/sessions/` | Gateway routing index (`sessions.json`), request-dump breadcrumbs, gateway `*.jsonl` transcripts, and optional per-session JSON snapshots when `sessions.write_json_snapshots: true`. Snapshots are off by default; state.db is canonical. |
| `~/.clover/cron/` | Scheduled job data |
| `~/.clover/whatsapp/session/` | WhatsApp bridge credentials |

---

## Architecture

### Core loop

```
User message → AIAgent._run_agent_loop()
  ├── Build system prompt (prompt_builder.py)
  ├── Build API kwargs (model, messages, tools, reasoning config)
  ├── Call LLM (OpenAI-compatible API)
  ├── If tool_calls in response:
  │     ├── Execute each tool via registry dispatch
  │     ├── Add tool results to conversation
  │     └── Loop back to LLM call
  ├── If text response:
  │     ├── Persist session to DB
  │     └── Return final_response
  └── Context compression if approaching token limit
```

### Design patterns worth knowing before you edit

- **Self-registering tools.** Each tool file calls `registry.register()` at import time. `model_tools.py` triggers discovery by importing all tool modules. There is no manual import list to maintain.
- **Toolset grouping.** Tools group into toolsets (`web`, `terminal`, `file`, `browser`) that can be enabled or disabled per platform.
- **Session persistence.** Conversations live in SQLite (`clover_state.py`) with full-text search and unique session titles. Per-session JSON snapshots in `~/.clover/sessions/` were superseded by the SQLite store and are off by default. Opt back in with `sessions.write_json_snapshots: true` if external tooling consumes the JSON directly.
- **Ephemeral injection.** System prompts and prefill messages are injected at API call time. They are never persisted to the database or logs.
- **Provider abstraction.** The agent works with any OpenAI-compatible API. Provider resolution happens at init time: Clover Portal OAuth, an API key, or a custom endpoint.
- **Provider routing.** On OpenRouter, `provider_routing` in config.yaml controls upstream selection (sort by throughput, latency, or price; allow or ignore specific providers; data retention policy). These become `extra_body.provider` in API requests.
- **The self-improvement loop.** After a turn, `spawn_background_review()` forks the agent onto a daemon thread with a tool whitelist limited to memory and skill management. It never touches the main conversation or the prompt cache. If you are changing anything here, read `references/self-improvement-loop.md` in the `clover-c1-dev` skill for the invariants and the review criteria.

---

## Code style

- **PEP 8**, with practical exceptions. We do not enforce strict line length.
- **Comments** explain non-obvious intent, trade-offs, or API quirks. Do not narrate the code. `# increment counter` adds nothing.
- **Error handling:** catch specific exceptions. Log with `logger.warning()` or `logger.error()`, and use `exc_info=True` for unexpected errors so stack traces reach the logs.
- **Cross-platform:** never assume Unix. See [Cross-platform compatibility](#cross-platform-compatibility).

---

## Adding a tool

First, check again: [should this be a skill?](#skill-or-tool)

Tools self-register. Each file co-locates its schema, handler, and registration:

```python
"""my_tool: Brief description of what this tool does."""

import json
from tools.registry import registry


def my_tool(param1: str, param2: int = 10, **kwargs) -> str:
    """Handler. Returns a string result (often JSON)."""
    result = do_work(param1, param2)
    return json.dumps(result)


MY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "What this tool does and when the agent should use it.",
        "parameters": {
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "What param1 is"},
                "param2": {"type": "integer", "description": "What param2 is", "default": 10},
            },
            "required": ["param1"],
        },
    },
}


def _check_requirements() -> bool:
    """Return True if this tool's dependencies are available."""
    return True


registry.register(
    name="my_tool",
    toolset="my_toolset",
    schema=MY_TOOL_SCHEMA,
    handler=lambda args, **kw: my_tool(**args, **kw),
    check_fn=_check_requirements,
)
```

**Wire it into a toolset. This is required.** Any `tools/*.py` file with a top-level `registry.register(...)` call is auto-imported by `discover_builtin_tools()` in `tools/registry.py` when `model_tools` loads. Discovery is automatic; exposure is not. Add the tool name to the right list in `toolsets.py` (for example `_CLOVER_CORE_TOOLS` or a dedicated toolset), or it will register cleanly and the agent will never see it. New toolset? Add it in `toolsets.py` and wire it into the relevant platform presets.

See `AGENTS.md`, section **Adding New Tools**, for profile-aware paths and plugin versus core guidance.

---

## Adding a skill

Bundled skills live in `skills/`, organized by category. Official optional skills use the same structure in `optional-skills/`:

```
skills/
├── research/
│   └── arxiv/
│       ├── SKILL.md              # Required: main instructions
│       └── scripts/              # Optional: helper scripts
│           └── search_arxiv.py
├── productivity/
│   └── ocr-and-documents/
│       ├── SKILL.md
│       ├── scripts/
│       └── references/
└── ...
```

### SKILL.md format

```markdown
---
name: my-skill
description: Brief description (shown in skill search results)
version: 1.0.0
author: Your Name
license: MIT
platforms: [macos, linux]          # Optional: restrict to specific OS platforms
                                   #   Valid: macos, linux, windows
                                   #   Omit to load on all platforms (default)
required_environment_variables:    # Optional: secure setup-on-load metadata
  - name: MY_API_KEY
    prompt: API key
    help: Where to get it
    required_for: full functionality
prerequisites:                     # Optional legacy runtime requirements
  env_vars: [MY_API_KEY]           #   Backward-compatible alias for required env vars
  commands: [curl, jq]             #   Advisory only; does not hide the skill
metadata:
  clover:
    tags: [Category, Subcategory, Keywords]
    related_skills: [other-skill-name]
    fallback_for_toolsets: [web]       # Optional: show only when toolset is unavailable
    requires_toolsets: [terminal]      # Optional: show only when toolset is available
---

# Skill Title

Brief intro.

## When to Use
Trigger conditions: when should the agent load this skill?

## Prerequisites
Env vars, install steps, MCP setup, API key sourcing.

## How to Run
Canonical invocation through the `terminal` tool.

## Quick Reference
Table of common commands or API calls.

## Procedure
Step-by-step instructions the agent follows.

## Pitfalls
Known failure modes and how to handle them.

## Verification
How the agent confirms it worked.
```

### Platform gating

Skills declare supported OS platforms via `platforms`. Gated skills are hidden from the system prompt, `skills_list()`, and slash commands on incompatible platforms.

```yaml
platforms: [macos]            # macOS only (iMessage, Apple Reminders)
platforms: [macos, linux]     # macOS and Linux
platforms: [windows]          # Windows only
```

Omit the field and the skill loads everywhere. See `skills/apple/` for macOS-only examples.

### Conditional activation

Skills can declare conditions based on which tools and toolsets exist in the current session. This is mostly for **fallback skills**: alternatives that should only appear when a primary tool is missing.

Four fields under `metadata.clover`:

```yaml
metadata:
  clover:
    fallback_for_toolsets: [web]      # Show ONLY when these toolsets are unavailable
    requires_toolsets: [terminal]     # Show ONLY when these toolsets are available
    fallback_for_tools: [web_search]  # Show ONLY when these specific tools are unavailable
    requires_tools: [terminal]        # Show ONLY when these specific tools are available
```

- `fallback_for_*`: the skill is a backup. Hidden when the listed tools or toolsets are available, shown when they are not. Use it for free alternatives to premium tools.
- `requires_*`: the skill needs those capabilities. Hidden when they are unavailable.
- Both specified: both must be satisfied.
- Neither specified: always shown.

```yaml
# DuckDuckGo search: shown when Firecrawl (web toolset) is unavailable
metadata:
  clover:
    fallback_for_toolsets: [web]

# Smart home skill: only useful when terminal is available
metadata:
  clover:
    requires_toolsets: [terminal]

# Local browser fallback: shown when Browserbase is unavailable
metadata:
  clover:
    fallback_for_toolsets: [browser]
```

Filtering happens at prompt build time in `agent/prompt_builder.py`. `build_skills_system_prompt()` receives the available tools and toolsets and evaluates each skill through `_skill_should_show()`.

### Setup metadata

Skills declare secure setup-on-load metadata via `required_environment_variables`. Missing values do not hide the skill from discovery. They trigger a CLI-only secure prompt when the skill is loaded.

```yaml
required_environment_variables:
  - name: TENOR_API_KEY
    prompt: Tenor API key
    help: Get a key from https://developers.google.com/tenor
    required_for: full functionality
```

The user can skip setup and keep loading the skill. Clover exposes only metadata to the model (`stored_as`, `skipped`, `validated`), never the secret value.

Legacy `prerequisites.env_vars` still works and normalizes into the new representation.

```yaml
prerequisites:
  env_vars: [TENOR_API_KEY]       # Legacy alias for required_environment_variables
  commands: [curl, jq]            # Advisory CLI checks
```

Gateway and messaging sessions never collect secrets in-band. They tell the user to run `clover setup` or edit `~/.clover/.env` locally.

Declare required env vars when the skill uses a key that should be collected securely at load time, and can still degrade gracefully if the user skips. Declare command prerequisites when the skill leans on a CLI that may not be installed (`himalaya`, `openhue`, `ddgs`); treat those as guidance, not discovery-time hiding.

See `skills/gifs/gif-search/` and `skills/email/himalaya/` for examples.

### Skill authoring standards (hardline)

Every new or modernized skill, bundled or optional or contributed, meets these before merge. Reviewers reject PRs that violate them.

1. **`description` is 60 characters or fewer, one sentence, ending in a period.** Long descriptions bloat the listing UI and dilute the model's attention when many skills load at once. State the capability, not the implementation. No marketing words ("powerful", "comprehensive", "seamless", "advanced"). Do not repeat the skill name. Verify:

   ```python
   import re, pathlib
   m = re.search(r'^description: (.*)$',
                 pathlib.Path('skills/<cat>/<name>/SKILL.md').read_text(),
                 re.MULTILINE)
   assert len(m.group(1)) <= 60, len(m.group(1))
   ```

   Good: `Search arXiv papers by keyword, author, category, or ID.`
   Bad: `A powerful and comprehensive skill that allows the agent to search arXiv for relevant academic papers using various criteria including keywords, authors, and categories.`

2. **Tools named in SKILL.md prose are native Clover tools or MCP servers the skill explicitly expects.** Point at the tool by name in backticks: `` `terminal` ``, `` `web_extract` ``, `` `web_search` ``, `` `read_file` ``, `` `write_file` ``, `` `patch` ``, `` `search_files` ``, `` `vision_analyze` ``, `` `browser_navigate` ``, `` `delegate_task` ``, `` `image_generate` ``, `` `text_to_speech` ``, `` `cronjob` ``, `` `memory` ``, `` `skill_view` ``, `` `todo` ``, `` `execute_code` ``.

   Do not name shell utilities the agent already has wrapped:

   | Don't say | Say |
   |---|---|
   | `grep`, `rg` | `search_files` |
   | `cat`, `head`, `tail` | `read_file` |
   | `sed`, `awk` | `patch` |
   | `find`, `ls` | `search_files` (with `target='files'`) |
   | `curl` for content extraction | `web_extract` |
   | `echo > file`, `cat <<EOF` | `write_file` |

   If the skill depends on an MCP server, name it and document setup under `## Prerequisites`. Third-party CLIs (`ffmpeg`, `gh`, a specific SDK) are fine inside script files, but the prose should frame it as "invoke through the `terminal` tool", not as a manual shell session.

3. **`platforms:` gating is audited against actual script imports.** Skills using POSIX-only primitives (`fcntl`, `termios`, `os.setsid`, `os.kill(pid, 0)` for liveness, `/proc`, hardcoded `/tmp` paths, `signal.SIGKILL`, bash heredocs, `osascript`, `apt`, `systemctl`) must declare `platforms:`. The default move is to fix it cross-platform first: `tempfile.gettempdir()`, `pathlib.Path`, `psutil.pid_exists()`, Python-level filtering instead of `grep`. Gate to a narrower set only when the dependency is genuinely platform-bound, like `osascript` on macOS or `/proc` on Linux.

4. **`author` credits the human first.** For external contributions, the contributor's real name plus GitHub handle goes first (`Jane Doe (jane-doe)`), with "Clover Cognition" as secondary collaborator. If a commit shows "Clover Cognition" as author because the contributor used Clover to draft the skill, replace it with their actual name. Credit the person, not the tool.

5. **The body uses the modern section order.** A `# <Skill> Skill` title, a 2 to 3 sentence intro covering what it does and what it does not do, then:
   - `## When to Use`, trigger conditions
   - `## Prerequisites`, env vars, install steps, MCP setup, API key sourcing
   - `## How to Run`, canonical invocation through the `terminal` tool
   - `## Quick Reference`, flat command and API reference
   - `## Procedure`, numbered steps with copy-paste commands
   - `## Pitfalls`, known limits, rate limits, things that look broken but are not
   - `## Verification`, a single command that proves the skill works

   Aim for roughly 200 lines for a complex skill and 100 for a simple one. Cut intro fluff, marketing prose, and re-explanations of env vars already covered in `## Prerequisites`.

6. **Scripts in `scripts/`, references in `references/`, templates in `templates/`.** Do not make the model inline-write parsers, XML walkers, or non-trivial logic on every call. Ship a helper script and reference it by path relative to the skill directory.

7. **Tests live at `tests/skills/test_<skill>_skill.py`** and use stdlib, pytest, and `unittest.mock` only. No live network calls. Run with `scripts/run_tests.sh tests/skills/test_<skill>_skill.py -q`. They must pass under the hermetic CI env, with no API keys leaking through. Use `monkeypatch` and `tmp_path` for env-var and filesystem dependencies.

8. **`.env.example` additions stay inside a clearly delimited block.** Do not touch the surrounding file. Contributor-supplied versions of `.env.example` are usually stale, and edits outside your skill's block get dropped during salvage. Comment all values with `#`, since this is documentation, not live config.

### General skill guidelines

- **Avoid external dependencies.** Prefer stdlib Python, curl, and existing Clover tools (`web_extract`, `terminal`, `read_file`).
- **Progressive disclosure.** Most common workflow first. Edge cases at the bottom.
- **Ship helper scripts** for parsing and complex logic.
- **Test it for real.** Run `clover --toolsets skills -q "Use the X skill to do Y"` and watch whether the agent actually follows your instructions. This is where most skills fall over.

---

## Adding a skin

The skin system is data-driven. New skins need no code changes.

**Option A: user skin.** Create `~/.clover/skins/<name>.yaml`:

```yaml
name: mytheme
description: Short description of the theme

colors:
  banner_border: "#HEX"     # Panel border color
  banner_title: "#HEX"      # Panel title color
  banner_accent: "#HEX"     # Section header color
  banner_dim: "#HEX"        # Muted/dim text color
  banner_text: "#HEX"       # Body text color
  response_border: "#HEX"   # Response box border

spinner:
  waiting_faces: ["(⚔)", "(⛨)"]
  thinking_faces: ["(⚔)", "(⌁)"]
  thinking_verbs: ["forging", "plotting"]
  wings:                     # Optional left/right decorations
    - ["⟪⚔", "⚔⟫"]

branding:
  agent_name: "My Agent"
  welcome: "Welcome message"
  response_label: " ⚔ Agent "
  prompt_symbol: "⚔"

tool_prefix: "╎"             # Tool output line prefix
```

Every field is optional. Missing values inherit from the default skin.

**Option B: built-in skin.** Add to the `_BUILTIN_SKINS` dict in `clover_cli/skin_engine.py`, same schema as a Python dict. Built-ins ship with the package and are always available.

**Activate:** `/skin mytheme` in the CLI, or `display: { skin: mytheme }` in config.yaml.

See `clover_cli/skin_engine.py` for the full schema and existing examples.

---

## Cross-platform compatibility

Clover runs on Linux, macOS, native Windows, and WSL2. Assume any platform can reach your code path.

> **Before you PR:** run `scripts/check-windows-footguns.py` against your diff. It is grep-based and cheap, and CI runs it on every PR anyway.

### Critical rules

1. **Never call `os.kill(pid, 0)` for liveness checks.** On POSIX, signal 0 is a no-op permission check and this is the standard idiom. **On Windows it is not a no-op.** Python's Windows `os.kill` maps `sig=0` to `CTRL_C_EVENT` (they collide at integer 0) and routes it through `GenerateConsoleCtrlEvent(0, pid)`, which broadcasts Ctrl+C to the **entire console process group** containing the target PID. "Probe if alive" silently becomes "kill the target, plus unrelated processes sharing its console." See [bpo-14484](https://bugs.python.org/issue14484), open since 2012 and never getting fixed, for compatibility reasons.

   **Use `psutil`** instead. It is a core dependency, so it is always there:

   ```python
   import psutil
   if psutil.pid_exists(pid):
       # process is alive: safe on every platform
       ...
   ```

   If you specifically need the Clover wrapper (it has a stdlib fallback for scaffold-phase imports before pip install finishes), use `gateway.status._pid_exists(pid)`. It calls `psutil.pid_exists` first and falls back to a hand-rolled `OpenProcess + WaitForSingleObject` dance on Windows only when psutil is somehow missing.

   Audit grep: `rg "os\.kill\([^,]+,\s*0\s*\)"`. Any hit in non-test code is presumptively a Windows silent-kill bug.

2. **Use `shutil.which()` before shelling out.** Windows does not have the tools Linux has. `wmic` was removed in Windows 10 21H1. `ps`, `kill`, `grep`, `awk`, `fuser`, `lsof`, `pgrep`, and most POSIX CLI tools do not exist there at all. Test with `shutil.which("tool")` and fall back to a Windows-native equivalent, usually PowerShell via `subprocess.run(["powershell", "-NoProfile", "-Command", ...])`.

   For process enumeration, PowerShell's `Get-CimInstance Win32_Process` replaces `wmic process`. See `clover_cli/gateway.py::_scan_gateway_pids` for the pattern.

3. **File encoding.** Windows may save `.env` files as `cp1252`. Handle it:

   ```python
   try:
       load_dotenv(env_path)
   except UnicodeDecodeError:
       load_dotenv(env_path, encoding="latin-1")
   ```

   Notepad and similar editors add a UTF-8 BOM to `config.yaml`. Use `encoding="utf-8-sig"` when reading files a Windows GUI editor could have touched.

4. **Process management.** `os.setsid()`, `os.killpg()`, `os.fork()`, `os.getuid()`, and POSIX signal handling all differ on Windows. Guard with `platform.system()`, `sys.platform`, or `hasattr(os, "setsid")`:

   ```python
   if platform.system() != "Windows":
       kwargs["preexec_fn"] = os.setsid
   else:
       kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
   ```

   To kill a process and its children (what `os.killpg` does on POSIX), use `psutil`. It works everywhere:

   ```python
   import psutil
   try:
       parent = psutil.Process(pid)
       # Kill children first (leaf-up), then the parent.
       for child in parent.children(recursive=True):
           child.kill()
       parent.kill()
   except psutil.NoSuchProcess:
       pass
   ```

5. **Signals that do not exist on Windows: `SIGALRM`, `SIGCHLD`, `SIGHUP`, `SIGUSR1`, `SIGUSR2`, `SIGPIPE`, `SIGQUIT`, `SIGKILL`.** Python's `signal` module raises `AttributeError` at import time if you reference them there. Use `getattr(signal, "SIGKILL", signal.SIGTERM)` or gate the block behind a platform check. `loop.add_signal_handler` raises `NotImplementedError` on Windows, so always catch it.

6. **Path separators.** Use `pathlib.Path`, not string concatenation with `/`. Forward slashes work almost everywhere on Windows, but `subprocess.run(["cmd.exe", "/c", ...])` and other shell contexts can require backslashes. Convert with `str(path)` at the subprocess boundary, not inside Python logic.

7. **Symlinks need elevated privileges on Windows** unless Developer Mode is on. Tests that create symlinks need `@pytest.mark.skipif(sys.platform == "win32", reason="Symlinks require elevated privileges on Windows")`.

8. **POSIX file modes are not enforced on NTFS.** Tests asserting on `stat().st_mode & 0o777` must skip on Windows, because the concept does not translate. Use ACLs (`icacls`, `pywin32`) for Windows secret-file protection if you need it.

9. **Detached background daemons on Windows need `pythonw.exe`, not `python.exe`.** `python.exe` always allocates or attaches to a console, which makes it vulnerable to `CTRL_C_EVENT` broadcasts from any sibling process. `pythonw.exe` is the no-console variant. Combine with `CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB` in `subprocess.Popen(creationflags=...)`. See `clover_cli/gateway_windows.py::_spawn_detached`.

10. **`subprocess.Popen` with `.cmd` or `.bat` shims needs `shutil.which` to resolve.** Passing `"agent-browser"` to `Popen` on Windows finds the extensionless POSIX shebang shim in `node_modules/.bin/`, which `CreateProcessW` cannot execute, and you get `WinError 193 "not a valid Win32 application"`. Use `shutil.which("agent-browser", path=local_bin)`, which honors PATHEXT and picks the `.CMD` variant.

11. **Shell shebangs are not a way to run Python.** `#!/usr/bin/env python` only works when a Unix shell executes the file. `subprocess.run(["./myscript.py"])` fails on Windows even with the shebang. Invoke Python explicitly: `[sys.executable, "myscript.py"]`.

12. **Installer changes come in pairs.** If you change `scripts/install.sh`, make the equivalent change in `scripts/install.ps1`. These two are the canonical example of "works on Linux" not meaning "works on Windows", and they have drifted more than once. Keep them in lockstep.

13. **Some Windows paths are OneDrive-redirected:** Desktop, Documents, Pictures, Videos. With OneDrive Backup enabled the real path is `%USERPROFILE%\OneDrive\Desktop` and friends, while `%USERPROFILE%\Desktop` still exists as an empty husk. Resolve the real location with `ctypes` plus `SHGetKnownFolderPath`, or read the `Shell Folders` registry key. Never assume `~/Desktop`.

14. **CRLF vs LF in generated scripts.** `cmd.exe` and `schtasks` parse line by line, and LF-only or mixed line endings break multi-line `.cmd` and `.bat` files. Use `open(path, "w", encoding="utf-8", newline="\r\n")`, or `open(path, "wb")` with explicit bytes, when generating scripts Windows will execute.

15. **Two quoting schemes in one command line.** `subprocess.run(["schtasks", "/TR", some_cmd])` means schtasks parses `/TR`, and then `cmd.exe` re-parses `some_cmd` when the task fires. Different parsers, different escape rules. Use two separate quoting helpers and never cross them. See `clover_cli/gateway_windows.py::_quote_cmd_script_arg` and `_quote_schtasks_arg` for the reference pair.

### Testing cross-platform

Tests that exercise platform-specific behavior have to run on their target platform.

```python
@pytest.mark.linux_only
@pytest.mark.macos_only
@pytest.mark.windows_only
```

Avoid monkeypatching `sys.platform` unless you have to. If you do, also patch `platform.system()`, `platform.release()`, and `platform.mac_ver()`. Symlinks, 0o600 permissions, SIGALRM, and `os.setsid`/`os.fork` are Unix-only.

---

## Security considerations

Clover has terminal access, so security is not a background concern here.

### Existing protections

| Layer | Implementation |
|-------|---------------|
| **Sudo password piping** | `shlex.quote()` to prevent shell injection |
| **Dangerous command detection** | Regex patterns in `tools/approval.py` with a user approval flow |
| **Cron prompt injection** | Scanner in `tools/cronjob_tools.py` blocks instruction-override patterns |
| **Write deny list** | Protected paths (`~/.ssh/authorized_keys`, `/etc/shadow`) resolved through `os.path.realpath()` to prevent symlink bypass |
| **Skills guard** | Security scanner for hub-installed skills (`tools/skills_guard.py`) |
| **Code execution sandbox** | `execute_code` child process runs with API keys stripped from the environment |
| **Container hardening** | Docker: all capabilities dropped, no privilege escalation, PID limits, size-limited tmpfs |

### When contributing security-sensitive code

- **Use `shlex.quote()`** whenever user input goes into a shell command.
- **Resolve symlinks** with `os.path.realpath()` before any path-based access control check.
- **Do not log secrets.** API keys, tokens, and passwords never appear in log output.
- **Catch broad exceptions** around tool execution so one failure does not take down the agent loop.
- **Test on all platforms** if your change touches file paths, process management, or shell commands.

Call it out explicitly in the PR description if your change affects security.

### Dependency pinning policy

After the [litellm supply chain compromise](https://github.com/BerriAI/litellm/issues/24512) in March 2026 and the [Mini Shai-Hulud worm campaign](https://socket.dev/blog/tanstack-npm-packages-compromised-mini-shai-hulud-supply-chain-attack) in May 2026, every dependency follows these rules:

| Source type | Required treatment | Rationale |
|---|---|---|
| **PyPI package** | `>=floor,<next_major` | PyPI versions are immutable once published, but new versions can land inside your range. A `<next_major` ceiling stops a 1.x install from jumping to a malicious 2.0.0. |
| **Git URL** (atroposlib, tinker, yc-bench, Baileys) | Full commit SHA | Branches and tags are mutable refs. A SHA is content-addressed. |
| **GitHub Actions** | Full commit SHA plus version comment | Action tags are mutable refs (see tj-actions/changed-files, March 2025). Pin as `uses: owner/action@<sha>  # vX.Y.Z`. |
| **CI-only pip installs** | `==exact` | Hermetic CI builds. Churn is acceptable. |

**Every new PyPI dependency needs a `<next_major` upper bound.** PRs with unbounded `>=X.Y.Z` specs get rejected. The `supply-chain-audit.yml` workflow also flags dependency manifest changes for manual review.

Choosing the ceiling:

- Package at `1.x.y`: use `<2`.
- Package at `0.x.y`: use `<0.(current_minor + 2)`. If current is `0.29.x`, use `<0.32`. That gives about two minor versions of headroom while keeping the window too small for a hostile takeover release to land inside it.
- Exception: packages with very stable APIs (`aiohttp-socks`) can use `<1` at reviewer discretion.

```toml
# ✅ Correct: post-1.0
"openai>=2.21.0,<3"
"pydantic>=2.12.5,<3"

# ✅ Correct: pre-1.0 (tight minor window)
"asyncpg>=0.29,<0.32"
"aiosqlite>=0.20,<0.23"
"hindsight-client>=0.4.22,<0.5"

# ❌ Rejected: no upper bound
"some-package>=1.2.3"

# ❌ Rejected: too tight (blocks legitimate patches)
"some-package==1.2.3"

# ❌ Rejected: too loose for pre-1.0 (allows 80 minor versions)
"some-package>=0.20,<1"
```

Reference PRs: #2796 (litellm removal), #2810 (upper bounds pass), #9801 (SHA pinning plus supply-chain-audit CI).

---

## Pull requests

### Branch naming

```
fix/description        # Bug fixes
feat/description       # New features
docs/description       # Documentation
test/description       # Tests
refactor/description   # Code restructuring
```

### Before submitting

1. **Run tests:** `scripts/run_tests.sh` (matches CI), or `pytest tests/ -v` with the project venv active.
2. **Test manually:** run `clover` and exercise the code path you changed. Tests passing is not the same as the feature working.
3. **Check cross-platform impact:** if you touched file I/O, process management, or terminal handling, think about macOS, Linux, and Windows.
4. **Keep it focused:** one logical change per PR. Do not mix a bug fix, a refactor, and a new feature.

### PR description

Include what changed and why, how to test it (repro steps for bugs, usage examples for features), which platforms you tested on, and any related issues.

### Commit messages

We use [Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <description>`

| Type | Use for |
|------|---------|
| `fix` | Bug fixes |
| `feat` | New features |
| `docs` | Documentation |
| `test` | Tests |
| `refactor` | Code restructuring, no behavior change |
| `chore` | Build, CI, dependency updates |

Scopes: `cli`, `gateway`, `tools`, `skills`, `agent`, `install`, `whatsapp`, `security`, and so on.

```
fix(cli): prevent crash in save_config_value when model is a string
feat(gateway): add WhatsApp multi-user session isolation
fix(security): prevent shell injection in sudo password piping
test(tools): add unit tests for file_operations
```

---

## Reporting issues

Use GitHub Issues. Include your OS, Python version, Clover version (`clover --version`), the full traceback, and steps to reproduce. Check existing issues first.

Report security vulnerabilities privately.

---

## Community

- **GitHub Discussions** for design proposals and architecture debates
- **Skills Hub** for sharing specialized skills

---

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
