<p align="center">
  <img src="assets/banner.png" alt="Clover Cognition" width="100%">
</p>

<h1 align="center">Clover Cognition 🍀</h1>

<p align="center">
  <a href="https://github.com/oaostarboy/clover-c1"><img src="https://img.shields.io/badge/Docs-read%20them-00D97E?style=for-the-badge" alt="Documentation"></a>
  <a href="https://github.com/oaostarboy/clover-c1/blob/main/LICENSE"><img src="https://img.shields.io/badge/License-MIT-2E8B57?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://github.com/oaostarboy/clover-c1"><img src="https://img.shields.io/badge/Built%20by-Anthony%20Nguyen-1B7A4B?style=for-the-badge" alt="Built by Anthony Nguyen"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/Lang-中文-2E8B57?style=for-the-badge" alt="中文"></a>
  <a href="README.ur-pk.md"><img src="https://img.shields.io/badge/Lang-اردو-2E8B57?style=for-the-badge" alt="اردو"></a>
  <a href="README.es.md"><img src="https://img.shields.io/badge/Lang-Español-2E8B57?style=for-the-badge" alt="Español"></a>
</p>

## An agent that remembers you

Most assistants forget you the moment you close the tab. You explain your setup, your
preferences, the way you like things done, and tomorrow you explain it all again.

Clover keeps notes. It writes down what it learns, turns hard-won solutions into reusable
skills, and searches its own past conversations when something sounds familiar. Give it a
month and it stops asking questions you already answered.

It also does not live on your laptop. Run it on a $5 VPS and talk to it from Telegram
while you are away from your desk. Close your laptop. It keeps working.

```
you    → the pdf contract in downloads, whats the termination clause
clover → 60 days written notice, either side, no penalty. page 4, section 9.2.
         same as the one you signed in March.
```

## What it actually does

| | |
|---|---|
| **Learns as it goes** | Writes its own skills after difficult tasks, then improves them the next time they run. Remembers across sessions, not just within one. |
| **A terminal you can live in** | Real TUI. Multiline editing, slash-command autocomplete, history, interrupt mid-thought and redirect, streaming tool output. |
| **Reaches you anywhere** | Telegram, Discord, Slack, WhatsApp, Signal, Email, all from one gateway. Send a voice note, get an answer. |
| **Works while you sleep** | Built-in scheduler. Morning briefings, nightly backups, weekly audits, described in plain language. |
| **Splits the work** | Spawns isolated subagents for parallel jobs. Writes Python that calls its own tools, so a ten-step pipeline costs one turn. |
| **Runs where you point it** | Local, Docker, SSH, Modal, Daytona, Vercel Sandbox. Serverless backends hibernate when idle and cost almost nothing between sessions. |
| **Your model, your choice** | 300+ models through one subscription, or bring your own key. Switch with `clover model`. No lock-in, no rewrite. |

---

## Install

**Linux, macOS, WSL2, Termux**

```bash
curl -fsSL https://raw.githubusercontent.com/oaostarboy/clover-c1/main/scripts/install.sh | bash
```

**Windows** (PowerShell, no WSL needed)

```powershell
iex (irm https://raw.githubusercontent.com/oaostarboy/clover-c1/main/scripts/install.ps1)
```

The installer brings its own everything: uv, Python 3.11, Node.js, ripgrep, ffmpeg, and a
portable Git Bash on Windows. No admin rights. It will not touch a Git you already have.

Then:

```bash
clover setup
```

Answer a few questions and you are talking to it.

<details>
<summary><b>Windows Defender flagged uv.exe</b></summary>

False positive. `uv.exe` is Astral's Rust package manager, and unsigned Rust binaries trip
heuristic scanners regularly. You can verify the download yourself:

```powershell
winget install --id GitHub.cli
gh auth login
$uv = "$env:LOCALAPPDATA\clover\bin\uv.exe"
$ver = (& $uv --version).Split(' ')[1]
Invoke-WebRequest "https://github.com/astral-sh/uv/releases/download/$ver/uv-x86_64-pc-windows-msvc.zip" -OutFile "$env:TEMP\uv.zip" -UseBasicParsing
gh attestation verify "$env:TEMP\uv.zip" --repo astral-sh/uv
```

If attestation passes, the binary is genuine. Add an exclusion and carry on.
</details>

<details>
<summary><b>Android / Termux</b></summary>

Termux installs a trimmed dependency set, because the full voice stack pulls libraries
Android cannot build. Everything else works. Run `clover doctor` after install to see
exactly what your device got.
</details>

---

## The commands worth knowing

```bash
clover                # start talking
clover --tui          # the full terminal interface
clover -c             # pick up the last conversation
clover model          # change the model
clover gateway        # run the messaging gateway
clover sessions list  # what have we talked about
clover doctor         # something is wrong, start here
clover update         # get the latest
```

Everything else is `clover <command> --help`.

---

## Two ways in

Start the terminal interface with `clover`, or run the gateway and message it from your
phone. Most slash commands work in both.

| | Terminal | Messaging |
|---|---|---|
| Start | `clover` | `clover gateway start`, then message the bot |
| Fresh start | `/new` | `/new` |
| Change model | `/model` | `/model` |
| Give it a personality | `/personality` | `/personality` |
| Undo that | `/retry`, `/undo` | `/retry`, `/undo` |
| Where did the context go | `/compress`, `/usage` | `/compress`, `/usage` |
| Stop | `Ctrl+C` | `/stop` |

---

## Bring your own keys

Clover talks to model providers directly. You hold the keys, and they stay on your
machine in `~/.clover/.env`.

```bash
clover setup        # walks through providers and stores the keys
clover model        # pick or change the default model
clover fallback     # set the chain to fall back to when one is down
```

Mix providers freely. It is per-backend, not all-or-nothing: one model for chat,
another for vision, your own local model for anything private.

---

## Learning more

The fastest documentation is the tool itself. Every command explains itself:

```bash
clover --help              # every command, with examples
clover <command> --help    # detail on one of them
clover doctor              # checks the install and tells you what is wrong
```

| Where | What is in it |
|---|---|
| [`docs/`](docs/) | Architecture notes, design records, security write-ups |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Repo layout, how to add a skill or a plugin |
| [`plugins/`](plugins/) | Every bundled plugin, each with its own README |
| [Discussions](https://github.com/oaostarboy/clover-c1/discussions) | Questions, ideas, showing what you built |
| [Issues](https://github.com/oaostarboy/clover-c1/issues) | Bugs, and things that should work better |

---

## Coming from OpenClaw

```bash
clover claw migrate
```

Brings your config, sessions, and memory across. It shows you the plan before it touches
anything.

---

## Contributing

Bug reports and pull requests are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first,
run the tests, and describe what you changed and why.

```bash
uv pip install -e ".[all,dev]"
scripts/run_tests.sh
```

---

## Licence

MIT. See [LICENSE](LICENSE).

Built by Anthony Nguyen at SILAS STUDIOS.
