# Terminal TLDR

A small native Mac app that gives your iTerm2 Codex and Claude tabs three emojis and a short,
useful title. Reviews use recent conversation context, not just the original name.

## The Mac app

Open **Terminal TLDR.app** for **Refresh titles** and Pause/Resume.
The single window has working **Model** and **Reasoning** selectors. Changes save
immediately and apply to the next review, without restarting the helper or
clearing title caches. The default is **GPT-6 Luna / Extra high**. Other choices
are GPT-6.1 Sol, GPT-6 Sol and GPT-6 Astra, subject to your Codex account access.
Reasoning ranges from Low to Maximum; delegated Ultra is deliberately excluded.
The More menu contains setup and account details.
It has a normal Dock icon and a menu-bar item. Closing the window keeps the app
available; quitting the app leaves the separately installed background helper
running. **Open at login** registers the native app with macOS Login Items; it
controls opening the app, separately from the automatic background helper.
The helper already starts at login and needs no open terminal window.
Pausing stops title updates until you explicitly resume, including
across logins. Neither action closes or sends input to a terminal.

1. Install iTerm2 and your usual Codex and/or Claude Code CLI.
2. Open the app and choose **Enable titles**.
3. In **iTerm Settings → General → Magic**, enable **Python API**. Approve the
   helper's connection if iTerm asks.
4. Sign in to the Codex CLI with `codex login` if you haven't already.
5. Optional: use **`/hooks`** in Codex to approve agent-written updates. Automatic
   reviews and the Refresh titles button work without these hooks.

The packaged app includes its Python runtime and dependencies. End users do not
need to install Python, run pip, or create an API key. The local Apple Silicon
build requires macOS 26 or later because its bundled Python runtime targets 26.
Verification so far is on macOS 27. Broader OS and Intel support are not claimed.

### Build and install from source

Requires Xcode Command Line Tools and Python 3.11 or later for packaging. Build
setup downloads the pinned packages in `macos/requirements-build.txt`.

```sh
bash scripts/build-app.sh
bash scripts/build-app.sh --install
open "/Applications/Terminal TLDR.app"
```

The app is built with SwiftPM, like the other native utilities in this collection.
The build produces `dist/Terminal TLDR.app` and an architecture-specific ZIP.
The installer refuses to replace a running app or an unrelated bundle, preserves
its stable `/Applications` path, and backs up an existing application first.
Installing the app itself does not start or reconfigure the background service;
that happens only when you choose setup in the app.

These are locally ad-hoc-signed builds, **not notarized public downloads**. Public
release, signing, dependency licence review and repository publication are
separate steps. The iTerm2 Python API declares GPLv2; see
[`macos/THIRD-PARTY-NOTICES.md`](macos/THIRD-PARTY-NOTICES.md) before choosing a
project distribution licence. No project licence is being changed implicitly.

## When titles change

- **New conversation:** its current task gets an initial review.
- **10 user messages or 60 minutes since review:** after new user activity,
  the selected model reviews a small excerpt of recent messages. Idle conversations
  don't trigger hourly model requests.
- **Same task:** the review preserves a title that still fits. Coding, testing
  and PR preparation are normally phases of one task.
- **Manual rename:** your chosen title wins until the foreground job changes.

The helper checks live session metadata every 15 seconds. Codex tabs are matched
through its local app-server to unambiguous main-thread identities; duplicate
names are refused. Claude tabs use its exact foreground-PID session registry.
Shared-daemon terminal environment variables are not used to guess a tab.
No agent is started, resumed, steered or interrupted.

**Refresh titles** reviews the current conversation tasks immediately. The app
reports how many titles changed, were already current, failed or couldn't be
matched. Requests are limited to once a minute; manual names stay untouched.

Optional trusted hooks let the primary agent update its title immediately on
a task change, with **no additional model request**. Their review resets the
automatic cadence. Hook trust is never bypassed. Codex automatic reviews require
its local app-server; Claude automatic reviews require its live PID/session registry.

## Native-name fallback

For sessions that have not supplied an agent-written title, the helper watches
iTerm's native task name. A new readable name gets one ephemeral request with the selected model
(default **`gpt-6-luna`**) through the user's installed Codex CLI. Titles are cached by native name.
Spinner, Working, Ready and Action Required changes are ignored.

Requests are serialized, tools and web search are disabled, and native-name requests
have a ten-second timeout. Current-conversation reviews have a thirty-second timeout. Failures back off for five then ten minutes, with three
attempts maximum. A fallback result never overwrites an agent-written title.
The title writer is event-driven; the conversation reviewer performs the bounded
local checks described above.

## Accounts and privacy

- Each person uses **their own installed Codex login**. This machine's tested
  path is Sign in with ChatGPT. Terminal TLDR has no shared account or backend.
- The app does not collect, copy or bundle authentication files. It never asks
  for a key. Existing Codex account settings remain outside the app and repo.
- Agent-written titles are local. Automatic/manual reviews send at most 6,000
  characters of recent user/assistant text to OpenAI through Codex, plus the
  current title. Native-name fallback sends the task name. These can contain
  sensitive task information. No images, tool output or full conversation is
  included in a title request.
- Excerpts stay in memory and are not written to Terminal TLDR's cache or logs.
  Codex excerpts come from bounded app-server turn summaries. Claude reads at
  most the last 2 MiB of its one PID-identified active transcript, ignores tool
  output and reuses an in-memory result while the file is unchanged. No history
  directory crawling or session-ledger changes.
- No analytics or telemetry. Local model preferences, title caches, counters, configuration backups
  and logs stay under `~/.terminal-tldr/`. Logs can include task names.
- Hooks are merged with existing Codex/Claude hooks. Other hooks, the session
  ledger and agent permissions are not replaced. Backups are private local files.
- The helper uses owner-only Unix sockets, validates the terminal and foreground
  job, and refuses stale or mismatched conversation writers. It never guesses
  the focused tab. This is not a security boundary against another process
  already running as the same OS user.

## CLI and development

The CLI remains useful for development; it isn't required to operate the app.

```sh
./bin/terminal-tldr install       # source install, including agent hooks
./bin/terminal-tldr doctor
./bin/terminal-tldr status
./bin/terminal-tldr refresh
./bin/terminal-tldr configure --model gpt-6-luna --reasoning xhigh
./bin/terminal-tldr pause
./bin/terminal-tldr resume
./bin/terminal-tldr uninstall
python3 -m unittest discover -s tests -v
```

`install --no-agents` skips the optional agent hooks; automatic reviews and the
native-name fallback still run. Uninstall removes
only this service, its launcher and its hook handlers. It leaves local caches,
config backups, logins, other hooks and terminal sessions intact. Older v2 title
caches are reused; obsolete pre-v2 state is neither read nor deleted.

The agent hook provides these commands with the exact conversation identifier:

```sh
~/.local/bin/terminal-tldr set --session SESSION_ID \
  --title "Keyboard Autocorrect" --emojis "⌨️" "🛠️" "✨"
~/.local/bin/terminal-tldr review --session SESSION_ID
```

The daemon is the only iTerm title writer. Native changes cannot replace an
agent-written title for the same owner. Session metadata survives a helper
restart and becomes invalid when the terminal foreground job changes.
Subagents are instructed not to update parent titles; mismatched Codex thread
identifiers and explicitly identified subagent hooks are rejected.

### Verification

The unit suite covers automatic and hook cadence boundaries, duplicate turn delivery, persistence,
saved model/reasoning selection, manual-title protection, stale ownership, existing-hook preservation, protocol
validation, title caching and subprocess cleanup. These are not a claim of live
UI verification or compatibility with every CLI version.

The opt-in live fixture targets only its own disposable iTerm window and private
pipe. It checks real title readback, agent updates without model calls, native
fallback generation, manual pins, warm restart and protected session identities:

```sh
~/.terminal-tldr/venv/bin/python scripts/verify_live.py
```

The conversation fixture creates one disposable Codex conversation and iTerm
window. It checks current-context generation, an automatic tenth-message
boundary (seeding the prior nine-message counter), and the actual Mac app's
Refresh titles button, then removes its fixture and verifies existing sessions were
preserved:

```sh
~/.terminal-tldr/venv/bin/python scripts/verify_conversations.py
```

Local screenshots and live records must remain in ignored `.local/` directories
or `~/.terminal-tldr/performance/`. Never publish a real desktop or conversation.

## Layout

- `Sources/TerminalTLDR/`: native SwiftUI application and menu bar.
- `lib/agent_titles.py`: per-terminal ownership and review cadence.
- `lib/conversations.py`: bounded Codex context, automatic reviews and live task matching.
- `lib/titles.py`: bounded fallback cache and isolated Luna request.
- `lib/setup.py`: setup, hook merging and explicit service management.
- `iterm2/terminal_tldr_iterm.py`: event-driven title writer and local socket.
- `bin/terminal-tldr`: CLI and lightweight hook entry point.
- `scripts/build-app.sh`: bundled runtime, SwiftPM build and local signing.

## Logo exploration

Ten proposed icon directions are in `design/logo-options/`, with the generation
prompts in `PROMPTS.md`. These are unselected concepts, not installed app assets.
The main window uses the same bundled icon as the Dock; the selected replacement
will be applied consistently after review.
