# hermes-imessage-shortcuts

**Text your Hermes agent over iMessage, from a Mac you already own.**

[![tests](https://github.com/ZackKuptsow/hermes-imessage-shortcuts/actions/workflows/tests.yml/badge.svg)](https://github.com/ZackKuptsow/hermes-imessage-shortcuts/actions/workflows/tests.yml)
![license: MIT](https://img.shields.io/badge/license-MIT-blue)
![python: 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![platform: macOS](https://img.shields.io/badge/platform-macOS-blue)

A platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that makes
Messages on your Mac the front door to your agent. Text it from your phone like any other
contact, and it answers in the same thread.

Hermes Agent already has iMessage options of its own: a
[BlueBubbles gateway](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/bluebubbles.md),
a hosted [Photon line](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/photon.md)
and an [`imsg` skill](https://github.com/NousResearch/hermes-agent/blob/main/skills/apple/imessage/SKILL.md).
This plugin is my take on the same idea, built to experiment with Hermes, n8n automations and
local LLMs.

## What you get

- **Two way iMessage.** Texts come in through a Shortcuts automation and replies go out through
  [imsg](https://github.com/openclaw/imsg), from the Mac's own Messages account.
- **Private by design.** Messages hands over one text at a time, so nothing the agent runs needs
  Full Disk Access or your message history.
- **Locked to you.** An allow list of senders, a shared secret, and a listener that answers only
  on the loopback address. Any one of them stops a stranger.
- **A text can only do so much.** Each text reaches two toolsets, messaging and memory. A terminal,
  a browser or a file tool is never on the other end of a text.
- **Proven in the runtime.** CI on macOS installs Hermes Agent with its own script and runs the
  suite inside it.

## How it works

```
phone  ──iMessage──▶  Messages.app on the Mac
                          │  Shortcuts automation, filtered to your handle
                          ▼
              POST http://127.0.0.1:8645/imessage   (X-Hermes-Secret)
                          │
                          ▼
              this plugin ──▶ Hermes gateway ──▶ the model
                          ◀── the reply
                          │  imsg send, from the Mac's Messages account
                          ▼
phone  ◀──iMessage──  Messages.app on the Mac
```

A Shortcuts "Message" automation on the Mac posts each incoming text to a listener the plugin
runs on the loopback address. Hermes answers, and the plugin sends the reply back through
[imsg](https://github.com/openclaw/imsg) from the same Messages account. Only handles on an
allow list get in or get a reply.

## What you need

- A Mac on a recent macOS (built and run on macOS 26) that stays on.
- Messages signed in. A dedicated Apple Account for the agent is strongly recommended, so the
  agent is not answering from your own account.
- Hermes Agent, with a profile for texting (below).
- imsg, from Homebrew: `brew install steipete/tap/imsg` (the project now lives at
  github.com/openclaw/imsg; read the formula's homepage at install time).
- A Shortcuts automation, configured by hand (step 7).

## Install

`P` is the texting profile's home, `~/.hermes/profiles/imessage`.

1. **The profile.** Made without `--clone`, so it copies no key or login from your default
   profile. A local model is the author's choice, not a requirement:

   ```sh
   hermes profile create imessage
   hermes -p imessage config set model.provider custom
   hermes -p imessage config set model.base_url http://127.0.0.1:11434/v1
   hermes -p imessage config set model.default qwen3.5:9b-q4_K_M
   ```

2. **The plugin, read in place.** Clone, then link the plugin directory (the repository root
   holds the tests and the scripts beside it):

   ```sh
   git clone https://github.com/ZackKuptsow/hermes-imessage-shortcuts ~/src/hermes-imessage-shortcuts
   mkdir -p ~/.hermes/profiles/imessage/plugins
   ln -s ~/src/hermes-imessage-shortcuts/imessage-shortcuts ~/.hermes/profiles/imessage/plugins/imessage-shortcuts
   hermes -p imessage plugins enable imessage-shortcuts
   ```

3. **The secret**, appended to the profile's `.env` without being shown. The leading newline
   keeps it off the file's last line:

   ```sh
   python3 -c 'import secrets; print("\nIMESSAGE_SHORTCUTS_SECRET=" + secrets.token_hex(32))' >> ~/.hermes/profiles/imessage/.env
   ```

   For the Shortcut's header field later:
   `grep '^IMESSAGE_SHORTCUTS_SECRET=' ~/.hermes/profiles/imessage/.env | cut -d= -f2- | pbcopy`.

4. **Your handle**, in E.164 (`+15551234567`) or as the email address Messages shows:
   - one per line in `P/data/imessage/zack-handles.txt`, made with `chmod 600`;
   - the same handles, comma-separated, in `P/.env` as `IMESSAGE_SHORTCUTS_ALLOWED_USERS=`.

   The adapter compares the two at start and refuses to run while they disagree.

5. **Toolsets.** Save the platform's list as exactly `imessage_shortcuts` and `memory`; the
   adapter refuses to start with any other:

   ```sh
   hermes -p imessage config set platform_toolsets.imessage_shortcuts '["imessage_shortcuts","memory"]'
   ```

6. **Restart the gateway** from another terminal (`hermes gateway restart`; a session running
   inside the gateway ends with it). Then `lsof -nP -iTCP:8645 -sTCP:LISTEN` should show
   `127.0.0.1` and nothing else.

7. **The automation**, in Shortcuts on the Mac: trigger Message, sender set to your contact
   card picked from Contacts, Message Contains left empty, Run Immediately, Allow Running When
   Locked. On macOS 26, with a typed handle as the sender the trigger would not save with
   Message Contains empty, and any text there (a single space) drops every one-word text.
   - Format Date: its Date set to Current Date, Date Format ISO 8601, Include ISO 8601 Time on.
     The action's own settings do the formatting, and the Current Date token inside it must carry
     none. The token's own popover is the usual trap:
     [Format Date](docs/troubleshooting.md#the-format-date-action).
   - Get Contents of URL: `POST` `http://127.0.0.1:8645/imessage`, header `X-Hermes-Secret`,
     a JSON body of `sender` (the sender's handle, not the contact's name), `sent_at` (Format
     Date's Formatted Date) and `text`.
   - No Send Message action: Hermes replies.

Send yourself a text. The first reply raises the Automation prompt for Messages; grant it to
the process it names, which is the gateway's, not a terminal's.

### What is not here

The secret, your handle, and the model choice live in the profile (`P/.env`, `P/config.yaml`,
`P/data/imessage/`). Nothing in this repository holds a value.

## Security in brief

A sender who is not you has to pass three gates, and any one stops them:

1. The automation fires only for the handle you filter it to.
2. The adapter drops every sender not on the allow list before the gateway sees the text.
3. A send to any handle off the list is refused, so not even a pairing code goes elsewhere.

The listener binds only the loopback address and checks the shared secret, in constant time,
before it reads a body. A text reaches two toolsets, `imessage_shortcuts` and `memory`, and the
adapter refuses to run if a setting or a Hermes upgrade would widen that.

The whole model is in [docs/security.md](docs/security.md), and every status code in
[docs/http-api.md](docs/http-api.md).

## Make it yours

Written for one person's Mac Studio and published as it runs there. The names in the code
are the author's: the allow list file is `zack-handles.txt`, the model's platform hint
addresses him by name, and the optional `household_forward` tool talks to a second service
he runs. The plugin works without that tool, and both are a one-line change
(`handles.py`, `registration.py`).

The optional tool, and the fixed phrases that go around the model, are in
[docs/household-forward.md](docs/household-forward.md).

## Layout

| Path | What it is |
|---|---|
| `imessage-shortcuts/` | the plugin: `adapter.py` (the loopback listener, `POST /imessage` for texts and `POST /imessage/send` for a text out without the model, each `204` saying why in `X-Imessage-Dropped`, and the replies), `handles.py` (canonical handles and the allow list), `allowed_toolsets.py` (the closed list of toolsets a text may reach), `inbound.py` (the request's shape), `imsg.py` (finding and calling imsg), `household.py` and `tools.py` (the optional forward), `fixed_phrases.py` (the texts that go to the household service around the model), `registration.py`, `plugin.yaml` |
| `bin/imessage-reply` | sends one iMessage, only to a handle on the allow list; a check that imsg works |
| `bin/household-forward` | forwards one command from a terminal; prints only whether the answer was delivered |
| `n8n/` | n8n as its own macOS user under a LaunchDaemon: `install.sh`, `watch-egress.sh`, the plist in `launchd/`; the relay workflow as a value-free template in `workflows/`, with `render.py`, `export.py`, `import.sh` and `n8n-cli.sh` to move it between git and the instance; `check_relay.py`, run at the Mac, which breaks each gate and reads the row it leaves; and its own `README.md` |
| `tests/` | the plugin's and the scripts' tests; `test_n8n_launchd.py`, which holds the n8n plist to the design; `test_n8n_install.py`, which runs `n8n/install.sh` step by step with root's commands stood in for; `test_n8n_workflows.py`, which refuses a relay that could store a text, run a command or hold a value, and covers the render and export scripts; `test_n8n_import.py`, which covers `import.sh` and `n8n-cli.sh` with sudo and n8n stood in for; and `test_n8n_check_relay.py`, which runs the check against a fake n8n told to misbehave in each way the check must catch. Beside it, `test_n8n_check_relay_workflow.py` covers the copy of the relay the check imports, `test_n8n_check_relay_stand_in.py` the plugin it stands in for, `test_n8n_check_relay_instance.py` its commands against a scratch n8n database, and `test_n8n_check_relay_table.py` the columns of its table; `n8n_check_support.py` holds what they share |
| `docs/` | [`security.md`](docs/security.md), [`http-api.md`](docs/http-api.md), [`troubleshooting.md`](docs/troubleshooting.md), [`household-forward.md`](docs/household-forward.md), and the n8n research and design |

## Tests

Most suites run under any Python 3.12 or later:

```sh
python3 -m unittest discover -s ./tests -p 'test_*.py'
```

The adapter's suite needs Hermes's own environment (`gateway`, `aiohttp`) and skips without
it. The full run is:

```sh
IMESSAGE_SHORTCUTS_TESTS_REQUIRE_HERMES=1 hermes --run-module unittest discover -s ./tests -p 'test_*.py'
```

With that variable set, the adapter's suite runs and never skips, so a run under the wrong
interpreter fails rather than passing on skipped tests. CI runs both forms on macOS, the
second after installing Hermes with its own install script.

Run both from the repository's root and keep the `./` in front of `tests`.
[Running the tests](docs/troubleshooting.md#running-the-tests) says why, and what not to do with
`HERMES_HOME`.

## Limits

Text only: no attachments, no typing indicator, and nothing delivers a cron job's output
yet. Shortcuts hands over the sender's handle in whatever written form it has; every phone
form is accepted, and a contact's name is refused rather than resolved, since imsg resolves a
name through Contacts and would send to whoever it matched.

## License

MIT.
