# hermes-imessage-shortcuts

A platform plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that lets
you text your agent over iMessage from your phone, using a Mac you already own, with no Full
Disk Access and no relay service in the middle.

A Shortcuts "Message" automation on the Mac posts each incoming text to a listener the plugin
runs on the loopback address. Hermes answers, and the plugin sends the reply back through
[imsg](https://github.com/openclaw/imsg) from the same Messages account. Only handles on an
allow list get in or get a reply.

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

Written for one person's Mac Studio and published as it runs there. The names in the code
are the author's: the allow list file is `zack-handles.txt`, the model's platform hint
addresses him by name, and the optional `household_forward` tool talks to a second service
he runs. The plugin works without that tool, and both are a one-line change
(`handles.py`, `registration.py`).

## Why Shortcuts, and why no Full Disk Access

Reading the Messages database directly needs Full Disk Access for whatever process reads it.
For an agent gateway that runs under launchd and can launch a shell, that is a broad grant.
The automation inverts it: Messages hands each text over, one HTTP request at a time, and
nothing Hermes launches is handed the database: the gateway's process is never given Full
Disk Access. The one permission the reply path needs is
Automation of Messages, for `imsg send`, and macOS asks for it the first time a reply goes out.

## What it refuses

A sender who is not you has to pass three gates, and any one stops them:

1. The automation fires only for the handle you filter it to.
2. The adapter drops every sender not on the profile's `data/imessage/zack-handles.txt`
   before the gateway sees the text. It refuses to start when that file and the gateway's
   allow list (`IMESSAGE_SHORTCUTS_ALLOWED_USERS`) disagree, or when the allow-all switch is on.
3. A send to any handle off the file is refused, so not even a pairing code goes elsewhere.
   Pairing is off for the platform.

The listener binds `127.0.0.1` or `::1` and refuses any other address, compares the shared
secret first and in constant time (at least 32 characters), and reads a body only once the
secret matches. Nothing logs a text, a handle or a secret.

Every `204` says why it dropped a request, in an `X-Imessage-Dropped` header holding exactly
one of `sender`, `empty` or `duplicate`, and no other answer carries it. On `POST /imessage`
those are a sender off the allow list, an empty text, and a repeat of a text already taken.
A caller such as an n8n relay can then alarm on a sender drop and let a safe retry pass.

`POST /imessage/send` sends one text without the model, so a loopback caller holding the
secret (n8n's alarms) can reach your phone through the gateway's existing Automation grant.
Its body is a JSON object of two strings, `to` and `text`. It passes the same three checks
first (`403` off the loopback address, `503` while the platform is not serving, `401`
without the secret), then `400` for a malformed body. The text leaves the way a reply does,
through gate 3: a recipient off the allow list is a `204` with `X-Imessage-Dropped: sender`,
and an empty text is a `400`, since the caller composed it and a `204` would read as
success. So is a text holding a NUL or a lone surrogate, which imsg cannot be handed or
Messages cannot show: it is refused whole, before any of a long text goes out, and never
sent altered. imsg never runs for any of these. `202` means imsg reported `sent`. `502` means
it did not, with imsg's word for why in a JSON body,
`{"disposition": "not_started"}`, and never the text. A text over 4,000 characters goes out
as several chunks, and when one after the first fails the body also says how far it got,
`{"delivered_chunks": 1, "disposition": "not_started", "total_chunks": 2}`.
`delivered_chunks` counts the chunks imsg reported `sent` before the one that failed, whose
own disposition still applies to it (with `may_have_completed` it may have arrived too). A
`502` carrying the counts is not safe to retry whole, whatever its disposition: a retry
would resend the chunks already sent. The endpoint opens no session and checks no
toolsets: no text reaches it, and the model has no tool that calls it. So while a widened
profile has every text answered `503`, the alarm about it still goes out, until the
platform next connects. A reconnect or a restart reruns the start checks, which refuse the
widened profile; from then on `/imessage/send` answers `503` too, or nothing listens, and
n8n's run record and its log are what report it.

One more guard, for anyone who runs more than one Hermes profile: the adapter starts only in
a profile named `imessage`, judged by the home Hermes gives it. The author's default profile
runs a cloud model and his texting profile runs a local one, and this is what keeps
`hermes plugins enable` in the wrong profile from handing his texts to the cloud model. The
profile name is `TEXTING_PROFILE` in `handles.py`.

And guards on what a text can do. A terminal, a browser or a file tool reached by text would
act with whatever access the gateway's process has, so a text reaches two toolsets,
`imessage_shortcuts` and `memory`, and nothing else:

- The adapter reads the profile's `config.yaml` the way the gateway does (managed overlay and
  `${VAR}` expansion applied) and refuses unless the platform's saved toolsets list
  (`platform_toolsets.imessage_shortcuts`) is exactly those two, no MCP server is configured
  (Hermes merges those into every platform's toolsets), and `memory.provider` is the built-in
  store (an external provider brings its own tools in with `memory`). With no saved list
  Hermes would hand the platform every core toolset. `allowed_toolsets.py` holds this part.
- It then asks Hermes's own resolver which toolsets it turns on for the platform, and refuses
  any beyond the two: a toolset shipped in a Hermes upgrade, or another enabled plugin's, is
  turned on without appearing in any setting.
- Both run at start, which refuses to listen, and again for every text, which is answered
  `503` and never reaches the gateway: the gateway rereads `config.yaml` each turn, so a list
  widened while it runs would otherwise reach the next text.
- Every text's toolsets are pinned to the two (`toolsets_for_source`), whatever the saved
  list says by then.
- A text's slash command runs only if it is `/new`, `/reset`, `/stop`, or `/approve` or
  `/cancel` in answer to `/new`'s confirmation. Any other, `/model`, `/kanban` or a skill's
  command among them, reaches the model as words.

Save the list with
`hermes -p imessage config set platform_toolsets.imessage_shortcuts '["imessage_shortcuts","memory"]'`.

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

5. **Toolsets.** Save the platform's list as exactly `imessage_shortcuts` and `memory`, the
   command under "What it refuses"; the adapter refuses to start with any other.

6. **Restart the gateway** from another terminal (`hermes gateway restart`; a session running
   inside the gateway ends with it). Then `lsof -nP -iTCP:8645 -sTCP:LISTEN` should show
   `127.0.0.1` and nothing else.

7. **The automation**, in Shortcuts on the Mac: trigger Message, sender set to your contact
   card picked from Contacts, Message Contains left empty, Run Immediately, Allow Running When
   Locked. On macOS 26, with a typed handle as the sender the trigger would not save with
   Message Contains empty, and any text there (a single space) drops every one-word text.
   - Format Date: its Date set to Current Date, Date Format ISO 8601, Include ISO 8601 Time
     on. The action's own settings do the formatting, and the Current Date token inside it
     must carry none. Clicking the token opens a popover whose Date Format and Time Format
     read Medium and Short when it has no format of its own: leave them. Set ISO 8601 there
     and Format Date is handed text instead of a date, keeps only the day (`2026-11-02`,
     without the time or offset), and the listener refuses every text. To clear a format
     already set, delete the token and pick Current Date again. Left as added, the action is
     handed the message instead and writes nothing, which is refused the same way. Either
     shows as a 400 with "the field 'sent_at' is not a moment" in the gateway log, the
     refusal for any `sent_at` without its time and offset: an empty one would give every
     repeat of a text one key, and Hermes answers the repeat with an error.
   - Get Contents of URL: `POST` `http://127.0.0.1:8645/imessage`, header `X-Hermes-Secret`,
     a JSON body of `sender` (the sender's handle, not the contact's name), `sent_at` (Format
     Date's Formatted Date) and `text`.
   - No Send Message action: Hermes replies.

Send yourself a text. The first reply raises the Automation prompt for Messages; grant it to
the process it names, which is the gateway's, not a terminal's.

### What is not here

The secret, your handle, and the model choice live in the profile (`P/.env`, `P/config.yaml`,
`P/data/imessage/`). Nothing in this repository holds a value.

## The optional forward tool

`household_forward` exists for the author's second service, a household assistant that answers
shared lists and reminders for two people. Hermes offers the tool only when
`P/data/imessage/household-function-url` exists; without that file the plugin is a plain
iMessage platform and the tool never appears.

When it is configured, the tool posts the command to that URL with a secret read from the login
Keychain (service `household-assistant-imessage-secret`), and the service's answer goes
straight to the phone through imsg. The answer is never returned to the model, because it can
carry the other person's data; the tool reports only whether it was delivered. The request
contract is `POST /` with a JSON body of `sender`, `sent_at` and `text` and the secret in
`X-Household-Assistant-Secret`. `bin/household-forward` is the same forward from a terminal.

## Layout

| Path | What it is |
|---|---|
| `imessage-shortcuts/` | the plugin: `adapter.py` (the loopback listener, `POST /imessage` for texts and `POST /imessage/send` for a text out without the model, each `204` saying why in `X-Imessage-Dropped`, and the replies), `handles.py` (canonical handles and the allow list), `allowed_toolsets.py` (the closed list of toolsets a text may reach), `inbound.py` (the request's shape), `imsg.py` (finding and calling imsg), `household.py` and `tools.py` (the optional forward), `registration.py`, `plugin.yaml` |
| `bin/imessage-reply` | sends one iMessage, only to a handle on the allow list; a check that imsg works |
| `bin/household-forward` | forwards one command from a terminal; prints only whether the answer was delivered |
| `tests/` | the plugin's and the scripts' tests |

## Tests

Most suites run under any Python 3.12 or later:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```

The adapter's suite needs Hermes's own environment (`gateway`, `aiohttp`) and skips without
it. The full run, 128 tests, is:

```sh
IMESSAGE_SHORTCUTS_TESTS_REQUIRE_HERMES=1 hermes --run-module unittest discover -s tests -p 'test_*.py'
```

With that variable set, the adapter's suite runs and never skips, so a run under the wrong
interpreter fails rather than passing on skipped tests. CI runs both forms on macOS, the
second after installing Hermes with its own install script.

Do not run these with `HERMES_HOME` pointed at a temporary directory: Hermes treats it as a
new data root, builds a runtime there, and re-executes the run under an interpreter without
`gateway`, which skips every adapter test and reads as green. The tests set the home through
`hermes_constants.set_hermes_home_override` instead.

## Limits

Text only: no attachments, no typing indicator, and nothing delivers a cron job's output
yet. Shortcuts hands over the sender's handle in whatever written form it has; every phone
form is accepted, and a contact's name is refused rather than resolved, since imsg resolves a
name through Contacts and would send to whoever it matched.

## License

MIT.
