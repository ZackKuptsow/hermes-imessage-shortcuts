# n8n routes the texts; Hermes stays the brain

- Date: 2026-10-30
- Status: draft, for Zack's approval
- Sources: the research report `.claude/research/reports/Moving from Shortcuts to n8n.md`
  and its three notes (read 2026-10-30); smart-home ADRs 0002, 0032, 0033, 0034, 0035, 0036;
  household-assistant ADR 0077 and research note 05; this repository's README and code.

## Intent and the why

Zack wants the plumbing of his texting path to live in one open, configurable, inspectable
tool he can learn and explain, instead of a Shortcuts automation he cannot read, version or
watch. In his words: Hermes is "the brain of the operation and n8n is just the method of
getting from point A to point B"; everything should be "under one roof, the n8n roof",
Shortcuts removed "but if that is dangerous we shouldn't"; the allow list is him and his
partner; and the learning is "more high level concepts than hands on the keyboard. I want to
know how the system works so I can read it back to someone as an automation project".

Today Shortcuts is only the doorbell: a Message automation posts each of Zack's texts to the
plugin's listener on `127.0.0.1:8645`, and the plugin does everything else. This design keeps
that doorbell as a three-action stub, because it is the one ingress on the Mac that needs no
privacy grant at all and filters by sender before any process of ours runs, and puts n8n
between the stub and the plugin as the switchboard. Hermes, the plugin and imsg are
unchanged.

## Goals, non-goals, anti-goals

Goals:

- Every decision about *where* a text goes is made in n8n, in one routing table, and the
  same table will carry the partner's route when her destination is settled.
- Every run leaves a record that says it happened, which route it took, how it ended and how
  long it took, with no text, handle or secret in it.
- A broken path raises an alarm instead of going quiet.
- The whole thing can be read back as an automation project: the story in one paragraph and
  one page, in n8n's own vocabulary.
- The workflows live in git as JSON, with a test that they cannot regress into storing text
  or running shell commands.

Non-goals:

- Replacing the Shortcuts Message automation as the detector. That is the Full Disk Access
  watcher, recorded below as a later, separate decision.
- Moving the Hermes turn, the toolset guards or the imsg reply out of the plugin. n8n never
  talks to a model and never sends a text in this design.
- Routing the partner's texts in the first delivery. Her route is designed in, wired when
  her Hermes-profile ADR lands and ADRs 0036 and 0077 are amended.
- Attachments, typing indicators, cron output: this repository's Limits section stands.

Anti-goals (this design has failed if any becomes true, even with everything working):

- Message text, a handle or a secret lands in n8n's database, n8n's log, a plist, or the
  repository.
- n8n, or anything it can start, holds Full Disk Access or Automation for Messages.
- Anything listens beyond `127.0.0.1`.
- A text can reach more inside Hermes than it can today.
- n8n phones home, even once, with telemetry, version checks or template fetches.

## Constraints

From the standing records:

- ADR 0002: local only. Every listener on `127.0.0.1`; nothing inbound to the house.
- ADR 0032 and 0035: the gateway's process holds Automation for Messages and never Full Disk
  Access; a text reaches two toolsets; nothing a text can reach may launch an app that holds
  Full Disk Access (Ghostty does).
- ADR 0033: "the Shortcut is built once, against the plugin". This design amends it: the
  Shortcut's URL and header change once more, to n8n's. A smart-home ADR records that before
  the cut-over.
- ADR 0034: the `imessage` profile answers Zack's texts on the Mac's Ollama; the plugin runs
  there alone.
- ADR 0036: the household secret's readers are the forward and the outbox fetch. n8n is not
  one until the partner's route is recorded.
- Household-assistant ADR 0077: the partner's texts never reach Hermes; her automation posts
  straight to the household function.
- ADR 0025's memory budget: about 3 GB of headroom remains for new services while the model
  is loaded.
- The repository's rule: nothing in it holds a value (a secret, a handle, a URL that is one).

From the research (dated 2026-10-30 in the report):

- n8n stable is 2.45.7; 3.0 is in beta, stable targeted for early December 2026, and drops
  npm installs for Docker. 2.x gets security and critical fixes for twelve months after.
- A Docker n8n cannot drive Messages.app; a native npm n8n can, through Execute Command,
  which is off by default in 2.x. This design needs neither, which is what keeps it
  portable to Docker n8n 3.x later.
- n8n saves every execution's full data by default for 336 hours; its Webhook node's header
  compare is exact but not constant-time; its listen address defaults to all interfaces;
  telemetry, version checks and template fetches are on by default.
- The plugin's listener answers 202 before the model runs, so the hop n8n waits on is
  milliseconds, not the model's 10 to 60 seconds.
- Hermes's API server exists and returns answers synchronously, but using it would move the
  toolset refusal into unchecked config and put the Automation grant on `node`. Rejected for
  this design; the report's guard table says why, line by line.

## The design

### The parts and the path

```
phone  ──iMessage──▶  Messages.app (assistant's Apple Account)
                          │  Shortcuts stub: on a message from the allow-listed contacts,
                          │  POST it. Three actions, no logic.
                          ▼
              n8n  http://127.0.0.1:5678/webhook/<path>      (X-Relay-Secret)
                          │  1. header secret        → 403 and stop
                          │  2. sender lookup        → route label, or "dropped"
                          │  3. route                → the destination for that label
                          │  4. run record           → one Data Table row, metadata only
                          ▼
              plugin  http://127.0.0.1:8645/imessage         (X-Hermes-Secret)
                          │  unchanged: canonical handle, allow list, toolset check,
                          │  dedup, 202, Hermes turn in the imessage profile
                          ▼
              Hermes ──▶ the model (Ollama) ──▶ household_forward / memory
                          │
                          ▼  imsg send, from the gateway's process (Automation only)
phone  ◀──iMessage──  Messages.app
```

Rollback is the Shortcut's URL and header set back to the plugin's. The plugin never stops
serving during the cut-over, since n8n relays to it rather than replacing it.

### What n8n decides, and what it never decides

n8n decides, from the sender alone: which route label a text carries, which destination that
label maps to, and whether to record and alarm. It never reads the text for any decision,
never calls a model, never runs a command, never sends a message. The plugin remains the
authority on the allow list (n8n's lookup is a routing table, not the gate), on
deduplication, and on everything inside Hermes.

### The routing table

One Set node at the top of the relay workflow holds the table at run time. Its source of
truth is not the editor: it is the handle files the plugin already reads (the `imessage`
profile's `data/imessage/zack-handles.txt`, mode 600, which dotfiles #132 made "the one
place Zack edits"; her profile's file later). Community n8n has no variables store and
`N8N_BLOCK_ENV_ACCESS_IN_NODE` also blocks `$env` in expressions, so the handles have to be
literal in the node; an import script renders the exported template with the files' values
and imports it, and the editor is read-only in practice. The same file feeding both means
the plugin's allow list and n8n's table cannot drift apart. Each row: the sender's
canonical handle (the Switch normalises what Messages hands over: punctuation stripped from
a number, an email lower-cased, as `handles.py` does), the route label, and the
destination's URL and credential name.

| Route label | Sender | Destination | When |
|---|---|---|---|
| `zack` | his handles | the plugin in the `imessage` profile, `127.0.0.1:8645` | first delivery |
| `partner` | her handles | the household function today; her own Hermes profile's listener when that ADR lands | after ADRs 0036 and 0077 are amended and she has agreed |
| `dropped` | anything else | nowhere; a run record with route `dropped` | always |

The table's values are not in the repository. The exported workflow JSON is a template:
it carries the row labels, credential names and template tokens; the handles are filled in on
the Mac at import from the handle files. "A value" means a handle, a secret, or a URL that
identifies a private service, such as the household function's. The plugin's own address,
`http://127.0.0.1:8645/imessage`, is published in the README and may appear in the export.

### The gates

The question Zack asked, "can we give n8n exactly what it needs", answered gate by gate.
macOS scopes processes, not data: there is no permission for "texts from these two handles",
which is why the detector stays with Apple. Every gate below is verified by breaking it and
watching it fail (the project's rule), and the check that does so is named in its ticket.

| Gate | What it constrains | How it fails closed |
|---|---|---|
| Shortcuts' Sender filter | Which texts leave Messages at all | A text from an unlisted sender runs no automation |
| Webhook Header Auth, `X-Relay-Secret`, 64 hex characters | Who can post to n8n | 403 before any node runs |
| Webhook IP allowlist `127.0.0.1, ::1` | Where a post may come from | 403 |
| `N8N_LISTEN_ADDRESS=127.0.0.1` | Where n8n can be reached | `lsof -nP -iTCP -sTCP:LISTEN -a -u _n8n` shows the loopback address alone, for 5678 and the task broker's 5679 alike |
| Routing table | Where a text can go | Unknown sender → `dropped`, no outbound call |
| Execute Command left excluded (n8n's 2.x default) | Whether any workflow can run a shell | The node is absent from the editor |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=true`, `N8N_RESTRICT_FILE_ACCESS_TO` | What a Code node could read | Access denied inside the node |
| n8n's SSRF protection on, `127.0.0.1/32` allowed | Where a workflow can connect inside the house | The LAN and link-local ranges are refused; the ticket verifies the plugin call still passes. n8n's switch cannot refuse the public internet: that needs the per-user firewall rule under "the n8n user" below |
| No Automation grant, no Full Disk Access for `node` | What n8n could do to Messages | macOS never prompts; the Full Disk Access and Automation lists are read with Zack after install |
| Plugin: secret, canonical allow list, toolset check per text, dedup | Everything past n8n | Unchanged; its 96 tests |
| Execution data saving `none`, instance-wide | Whether text is stored | The executions list is empty by design; the run record is a Data Table of five metadata fields |
| `N8N_PUBLIC_API_DISABLED=true` | Whether a second API, and a third secret, exist on the listener | The `/api/v1` routes answer 404; the heartbeat reads the Data Table instead |
| Secrets only in n8n's encrypted credential store and the plugin's `.env` | Where a secret can be read | None in the plist, the JSON or the repository; a test greps the export |

What this design does not gate, said plainly: whoever uses Zack's unlocked session can open
n8n's editor with the owner password and change the routing table, as they can edit the
Shortcut today. n8n is a new process with about a thousand npm dependencies and a Code node
that runs JavaScript in-process; no community node is installed, the version is pinned, and
upgrades are a deliberate step.

**The n8n user.** This relay never sends Apple Events, so n8n does not need Zack's login
session. Decision (Zack, 2026-10-30): it runs as a macOS user of its own, `_n8n`, a role
account with its home at `/var/n8n`, started by a LaunchDaemon with `UserName _n8n`. That
is the nearest thing macOS has to an IAM role: n8n cannot read Zack's files, the Hermes
profiles, the login Keychain or the handle files (the import script, run by Zack, hands it
the rendered table and removes the rendered copy), and a packet-filter anchor refuses every
outbound connection for that user except the loopback interface, the egress gate n8n's own
switch cannot provide. Two more rows for the gate table:

| Gate | What it constrains | How it fails closed |
|---|---|---|
| `_n8n`, its own user and home | What n8n can read on the Mac | `sudo -u _n8n cat` of the handle file, a profile's `.env` and the Keychain item all fail |
| pf anchor: `pass out on lo0 user _n8n`, `block out user _n8n` | Where n8n can connect, at the kernel | `curl` to any address from `_n8n` fails; to `127.0.0.1:8645` connects. The anchor's loader re-adds its line to `/etc/pf.conf` at every boot, since a macOS update can replace that file |

### Run records without text, alarms, and a heartbeat

- **Run records.** The instance saves no execution data. The relay's last node inserts one
  row into a Data Table, `run_record`: `received_at`, `route`, `outcome`, `plugin_status`,
  `duration_ms`. The HTTP Request node continues on error so `plugin_unreachable` and
  `timeout` reach that row too. Data Tables ship in every n8n plan with a 50 MB instance
  cap ([release notes](https://docs.n8n.io/reference/release-notes), [data tables](https://docs.n8n.io/data/data-tables/),
  read 2026-10-30); the heartbeat deletes rows older than 7 days. If Community 2.45 turns
  out not to have them, the fallback is a sub-workflow whose saved executions are the log,
  read through the public API with a key, which is the third secret this design avoids.
  The outcomes, a closed mapping: `accepted` (plugin 202), `dropped` (routing table miss),
  `dropped_by_plugin` (204: an off-list sender, an empty text or a duplicate; the plugin
  does not say which), `secret_refused` (401), `plugin_refusing` (any other HTTP status:
  503 not serving, 400 a broken body, 403 a non-loopback peer, anything unexpected),
  `plugin_unreachable` (the connection failed), `timeout` (it ran out of time). The real
  status always rides in `plugin_status`. No sender fingerprint: a hash of a phone number
  is a small keyspace and reverses.
- **Alarms.** An Error Workflow catches any failed relay execution, and the relay alarms on
  `secret_refused`, `plugin_refusing`, `plugin_unreachable` and `timeout`. `dropped_by_plugin`
  is recorded and not alarmed: it is also what a safe retry and an empty text produce, and
  the drift it could hide (table and allow list disagreeing) is prevented by rendering both
  from one file. When the plugin changes in phase 4 it adds a drop-reason header, and the
  rule alarms on a sender drop alone. The alarm needs a way to tell Zack without going
  through the model. First delivery: the row and a line in n8n's log, read with `tail`.
  Second delivery: a `send` endpoint on the plugin's listener (`POST /imessage/send`, same
  secret, recipient refused unless on the allow list) lets n8n text Zack "the texting path
  failed: plugin answered 503 at 17:02" from inside the trust boundary, with no new grant.
  That endpoint is also the egress any future automation would use; it is a plugin change
  with its own tests, and it waits until the relay has run.
- **Heartbeat.** A Schedule trigger reads `run_record` every morning: if no `accepted` run
  from route `zack` happened in the last 36 hours, alarm. A system that only alarms on
  errors cannot see silence. The floor under it is a daily "ping" from Zack's phone, which
  also proves the Shortcuts trigger still fires, the thing no 2026 source could promise.
  Decision (Zack, 2026-10-30): the ping goes all the way. It is forwarded like any text,
  Hermes answers it from the local model and imsg replies, so every morning proves the
  whole chain to the phone, and a reply every morning is the accepted cost. n8n reads no
  text, with no exception; the alternative of a `ping` rule in n8n was offered and not
  taken. 36 hours is a starting value.

### Secrets

- `X-Relay-Secret`: new, 64 hex characters, generated on the Mac. Lives in the Shortcut's
  header field and in an n8n Header Auth credential named `Shortcuts relay secret`.
- The plugin's `IMESSAGE_SHORTCUTS_SECRET`: unchanged in the profile's `.env`; a copy in an
  n8n Header Auth credential named `Hermes iMessage plugin secret`.
- n8n encrypts credentials with a key it writes to `/var/n8n/.n8n/config` (mode 600) on
  first start. Under `_n8n` that file is readable by `_n8n` and root alone, which is
  narrower than the profile's `.env` in Zack's home. It is the one file that can decrypt the
  credentials, so the runbook names it as the one file to back up. Nothing goes in the
  LaunchDaemon plist, which is readable by every user on the Mac.
- The household secret stays where ADR 0036 puts it until the partner's route is recorded.
- There is no third secret: the public API is off, so no API key exists, and the heartbeat
  reads the Data Table from inside n8n.

### Install, service and hardening

Native n8n 2.x from npm, pinned (`n8n@2.45.x` at the time of writing), run by a
LaunchDaemon as the `_n8n` user. Chosen over Docker because Docker Desktop's VM takes an
unmeasured share of the 3 GB headroom, and because this workflow runs no shell command, so
it moves to Docker n8n 3.x unchanged when the 2.x window closes (about December 2027; the
revisit trigger below). A daemon rather than an agent because nothing here needs the GUI
session: no Apple Events, no prompts.

The daemon's environment, every variable in the plist's `EnvironmentVariables` since
launchd ignores `.env` files:

| Variable | Value | Why |
|---|---|---|
| `N8N_LISTEN_ADDRESS` | `127.0.0.1` | default is every interface |
| `N8N_PORT` | `5678` | |
| `N8N_HOST`, `N8N_EDITOR_BASE_URL`, `N8N_WEBHOOK_URL` | `127.0.0.1`, `http://127.0.0.1:5678/` | `WEBHOOK_URL` is deprecated since 2.35 |
| `N8N_SECURE_COOKIE` | `false` | WebKit refuses Secure cookies over plain http on localhost |
| `N8N_PROTOCOL` | `http` | plain http on the loopback interface |
| `N8N_DIAGNOSTICS_ENABLED`, `N8N_VERSION_NOTIFICATIONS_ENABLED`, `N8N_TEMPLATES_ENABLED`, `N8N_PERSONALIZATION_ENABLED` | `false` | nothing leaves the Mac |
| `N8N_DIAGNOSTICS_CONFIG_FRONTEND`, `N8N_DIAGNOSTICS_CONFIG_BACKEND`, `EXTERNAL_FRONTEND_HOOKS_URLS` | empty | same: n8n's isolation page lists all three beside the switches above |
| `N8N_DISABLED_MODULES` | `insights` | the Insights module counts production runs for a dashboard this instance does not need |
| `N8N_LICENSE_AUTO_RENEW_ENABLED`, `N8N_LICENSE_SERVER_URL` | `false`, `http://127.0.0.1:9/` | whether an unlicensed Community instance calls the license server is undocumented; a dead loopback address makes any attempt fail at once rather than hang on the firewall |
| `EXECUTIONS_DATA_SAVE_ON_SUCCESS`, `EXECUTIONS_DATA_SAVE_ON_ERROR` | `none` | no workflow overrides it; the run record is a Data Table |
| `EXECUTIONS_DATA_SAVE_MANUAL_EXECUTIONS`, `EXECUTIONS_DATA_SAVE_ON_PROGRESS` | `false` | |
| `EXECUTIONS_DATA_PRUNE`, `EXECUTIONS_DATA_MAX_AGE` | `true`, `168` | belt and braces; nothing should be there to prune |
| `N8N_PUBLIC_API_DISABLED` | `true` | one fewer surface, no API key |
| `NODES_EXCLUDE` | absent | n8n's 2.x default excludes Execute Command and the local file trigger; the test asserts the row is absent, as it does for `N8N_ENCRYPTION_KEY` |
| `N8N_COMMUNITY_PACKAGES_ENABLED`, `N8N_PYTHON_ENABLED` | `false` | no third-party nodes, no second runtime |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | `true` | also blocks `$env` in expressions, which is why the table is rendered |
| `N8N_RESTRICT_FILE_ACCESS_TO` | `/var/n8n/.n8n` | with `N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES` at its default `true`, the pair leaves a node no file access anywhere, which is the intent |
| `N8N_SSRF_PROTECTION_ENABLED`, `N8N_SSRF_ALLOWED_IP_RANGES` | `true`, `127.0.0.1/32` | the LAN refused, the plugin allowed; verified both ways |
| `N8N_LOG_LEVEL`, `N8N_LOG_OUTPUT`, `N8N_LOG_FILE_LOCATION` | `info`, `file`, `/var/n8n/log/n8n.log` | readable by the admin group so Zack can `tail` it; n8n's webhook logger never prints bodies |
| `NODE_OPTIONS` | `--max-old-space-size=512` | a cap under the budget; measured in the first ticket |
| `N8N_USER_FOLDER` | `/var/n8n` | n8n appends `.n8n` itself (`getN8nFolder()` in `@n8n/config`), so the data lands in `/var/n8n/.n8n`: SQLite database, config, credentials key, Data Tables; `_n8n` and root alone |
| `HOME` | `/var/n8n` | launchd sets no `HOME` for a `UserName` job |
| `PATH` | `/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin` | the daemon starts n8n by absolute path; `PATH` is for whatever n8n spawns |

The rest of the plist: `ProgramArguments` of `/opt/homebrew/bin/node`, `/var/n8n/npm/lib/node_modules/n8n/bin/n8n`, `start`; `UserName` and `GroupName` `_n8n`; `RunAtLoad` and `KeepAlive` true; `ThrottleInterval` 30; `WorkingDirectory` `/var/n8n`; `ProcessType` `Background`; `Umask` `027`; `StandardOutPath` and `StandardErrorPath` under `/var/n8n/log/`. The user is a role account, UID and GID 450 (macOS requires 450 to 499 and a leading underscore), its own group made first, shell `/usr/bin/false`, home `/var/n8n` at mode 750 and group `admin` so Zack can read the log without `sudo`. Homebrew's `node` is the runtime, by absolute path: a `brew upgrade node` is a restart risk the status check catches, and `/opt/homebrew/bin` is writable by the admin group, which on this Mac is Zack.
| the license variables | confirmed against the environment reference in phase 1 | whether a Community instance contacts the license server is undocumented; if the day-long egress watch sees it, auto-renew is turned off or its server URL pointed at a dead loopback address |

The owner account is created once in the editor, before any workflow is imported, with
telemetry already off through the plist so first launch makes no outbound call.

### Workflows as code, and their tests

The three workflows (`iMessage relay`, `Relay failed`, `Inbound heartbeat`) live as
templates under `n8n/workflows/` in this repository, authored in the editor and exported,
never hand-written, then stored canonically (keys sorted, two-space indent). The
`run_record` Data Table is created once by hand in the editor, since n8n's CLI imports
workflows and not tables; its id is a template token like the handles. A render script
fills the tokens from the handle files; an import wrapper renders into a place the n8n user
can read, imports with n8n's CLI and removes the rendered copy; an export script strips the
instance-specific keys and puts the tokens back, so a round trip leaves git value-free.
Credentials are referenced by name, never exported. The webhook path is the readable
constant `imessage-relay`: header auth is the gate, and a random path would be one more
string that looks like a secret. A Python test under `tests/` parses each template and refuses: a node of the
Execute Command, Code, SSH or local file types; any node that could run a command or read
a file outside the Data Table; settings that save successful or failed executions; any
string that looks like a handle, a secret or a private URL; a credential embedded inline;
a template token missing from the relay. A second check, run on the Mac after import, posts to
the webhook with no secret, a wrong secret, an unknown sender, a known sender with the
plugin stopped, and a known sender with a stand-in plugin, and reads the expected outcome
row from `run_record` each time. Each gate in the table above is broken once during that
check and seen to fail.

### The Shortcut stub

Trigger: Message, sender the contact cards of the allow-listed people (one automation per
sender if the trigger will not take two), Message Contains empty, Run Immediately, Allow
Running When Locked. Actions: Format Date (ISO 8601 with time), Get Contents of URL (`POST
http://127.0.0.1:5678/webhook/<path>`, header `X-Relay-Secret`, JSON body `sender`,
`sent_at`, `text`), nothing else. The body is the same contract the plugin and the
household function already take, so n8n passes it through unchanged.

## Decisions, and whose they are

Zack's, in this conversation (2026-10-30):

- Hermes is the brain; n8n is the method from A to B.
- Shortcuts stays rather than anything dangerous; the watcher is not in scope.
- The allow list is him and his partner; routing between them is a goal.
- No hosted iMessage vendor.
- Learning at the level of concepts and the story, not the keyboard.
- n8n runs as its own macOS user with the packet-filter rule, the closest macOS gets to the
  permission set he asked for.
- The heartbeat ping goes all the way to a reply every morning; n8n reads no text, ever.

Mine, in this design, each one his to overturn:

- n8n routes and records; the plugin stays the trust boundary and the sender (the report's
  guard table is the reason).
- Native n8n 2.x now, Docker later, for the memory budget and portability.
- The instance saves no execution data and the run record is a Data Table of five
  metadata fields, rather than saved error executions, because an error execution holds
  the text, and rather than a sub-workflow's executions list, because reading that from
  the heartbeat needs the public API and a third secret.
- The routing table's values come from the plugin's handle files at import, so one file
  feeds both the plugin's allow list and n8n's table, and the exports stay value-free.
- The plugin's 204 is recorded, not alarmed, until phase 4 gives it a drop-reason header.
- The alarm's second stage is a `send` endpoint on the plugin, not Execute Command in n8n.
- The cut-over ADR is drafted by the building session in smart-home's `docs/decisions/`
  (the next number after 0036, in that repository's format) and accepted by Zack, as the
  earlier records were. The partner's ADRs (her Hermes profile; amendments to smart-home
  0036 and household-assistant 0077) are outside this plan: phase 5 waits on them.
- The partner's route is designed in and wired after her ADRs.
- Everything n8n-related lives in this repository under `n8n/` (exports, the plist
  template, the install and check scripts, a README section), so the project is one thing
  to read back. The Ollama precedent would put the plist in smart-home's installer instead;
  that is a move, not a redesign, if Zack prefers it.
- A daily phone-side "ping" is the heartbeat's floor; 36 hours is its window.
- The webhook path is the fixed, readable `imessage-relay`; header auth is the gate, and a
  random path would only look like a secret to the test that refuses secrets.

## Left to the builder

- Exact node choices inside the relay (Switch versus IF chains, how the routing table is
  shaped as items), as long as the routing table stays one rendered Set node.
- The run record's exact field names, the heartbeat's query, and the Data Table's columns.
- The render and export scripts' shape and template token syntax.
- The install script's shape and whether it shares code with dotfiles' `setup.sh`.
- The Python test's heuristics for "looks like a value".
- The `send` endpoint's request shape, following `inbound.py`'s style.

## Telling it as an automation project

In one paragraph: Messages on the Mac is the doorbell. When a text arrives from one of the
two allow-listed people, a three-step Shortcut rings it by posting the text to n8n on the
Mac's loopback address. n8n is the switchboard: it checks the caller's secret, looks the
sender up in a routing table, forwards the text to that person's agent, and writes down that
a call happened, when, which line it took and how it ended, without writing down what was
said. Hermes is the brain behind each line: it reads the text, answers from a local model,
forwards household commands to the household assistant, and remembers. imsg is the mouth:
Hermes speaks the reply back through the same Messages account. Every door is on the
loopback address, every secret is a long random string checked before anything runs, and
the only process that ever reads the Messages database is Apple's own.

In one page, the concepts in n8n's words, in the order they appear in the workflow:

1. **Trigger.** Every workflow starts at one trigger node. Ours is a Webhook, which turns
   an HTTP POST into a run. Shortcuts is the thing that posts; it is not part of n8n.
2. **Items.** n8n passes data between nodes as a list of items, each a JSON object. The
   webhook produces one item holding the body, headers and query. Every later node
   transforms that list.
3. **Credentials.** Secrets live in n8n's encrypted credential store and are referenced by
   name. The webhook checks `X-Relay-Secret` against one credential; the outbound request
   sends the plugin's secret from another. The exported JSON names them and never holds them.
4. **Expressions.** Any field can be computed from the item with `{{ }}`. The routing
   table is a Set node of constants; a Switch node compares the sender against it and sends
   the item down one branch.
5. **Routing.** A Switch node is the switchboard proper: one input, several outputs, a rule
   per output, a fallback output for "none matched", which here is the `dropped` branch.
6. **HTTP Request and idempotency.** The forward is an HTTP Request node to the plugin.
   It gets 202 in milliseconds because the plugin answers before Hermes thinks. If the
   request is retried after a timeout, the plugin's duplicate detection throws the second
   copy away, so a retry can never produce two replies. That property is called
   idempotency, and it is what makes retrying safe.
7. **Data Tables and data retention.** n8n normally keeps every run's full data; this
   instance keeps none. Instead the relay's last node writes five fields of metadata into
   a Data Table, n8n's built-in small database, and the heartbeat reads it back and prunes
   it. The result is a log that proves every run and contains no text, which is the
   privacy rule expressed as a workflow shape rather than a promise.
8. **Error workflows.** A workflow can name another to run when it fails. "Relay failed"
   receives the failing run's id, workflow name and error message, and raises the alarm.
9. **Schedule triggers and dead-man checks.** "Inbound heartbeat" runs on a timer and asks
   a negative question: has nothing arrived for 36 hours? A system that only alarms on
   errors cannot see silence; a heartbeat can.
10. **Least privilege, macOS style.** Cloud IAM scopes data; macOS scopes processes and
    users. The design grants n8n nothing: no Full Disk Access, no Automation, and it runs
    as a user of its own that owns only its database, with a kernel firewall rule that lets
    it talk to the Mac itself and nothing else. The only process with a privacy grant is
    Hermes's gateway, and it can only send. Each gate is tested by breaking it.
11. **Workflows as code.** The CLI exports each workflow as JSON; git holds the exports; a
    test refuses an export that could store text or run a command. Changing the routing is
    a diff anyone can read.

## Phases

1. **n8n on the Mac as `_n8n`, loopback-only, hardened, fenced, measured.** The role
   account, the LaunchDaemon with the plist table above, the owner account, the
   packet-filter anchor and its loader, `lsof`, a day's egress watch, the memory reading
   against the budget, the permission lists read with Zack. Nothing routes yet.
2. **The relay, run record and tests.** The three workflows and the Data Table as
   templates, the render and export scripts, the Python test, the on-Mac check with a
   stand-in plugin, every gate seen to fail.
3. **Cut-over.** The smart-home ADR amending 0033, drafted by the session and accepted by
   Zack; the Shortcut's URL and header changed by Zack at the Mac; the first real text
   through n8n; the heartbeat and phone ping armed.
4. **The alarm's second stage.** The plugin's `send` endpoint with tests; "Relay failed"
   texts Zack.
5. **The partner's route.** Waits on her Hermes-profile ADR and the amendments to 0036 and
   0077; then one row in the routing table and her Shortcut's URL.

Phase 1 is the baseline every later phase lands on. Phases 4 and 5 are independent of
each other.

## Revisit when

- n8n 2.x's security window closes (about December 2027), or n8n ships a 2.x fix that
  needs 3.0: move to Docker n8n with the same exports; the plugin call must then be tested
  from the container to the loopback listener.
- The Shortcuts Message trigger misses or delays texts (the heartbeat shows it): the
  watcher decision, as its own ADR, with the partner's agreement, a purpose-built process
  holding Full Disk Access that n8n cannot start, and a sandbox profile that limits it to
  the Messages database and the n8n port.
- Any automation other than the texting path wants to send an iMessage: it uses the
  plugin's `send` endpoint; a second consumer is the moment to consider whether that
  endpoint belongs in its own small service.
- n8n's license, or Zack's reading of "open source", makes Node-RED (Apache-2.0, native,
  exec node) preferable: the design is tool-agnostic above the webhook; the exports are not.

## Open questions only the Mac can answer

Each is a test in a ticket, not a lookup.

| Question | Test |
|---|---|
| Does n8n 2.45.x start and pass its own health check on Node 26.10.0? | first start in phase 1 |
| Idle and loaded memory of native n8n on Apple silicon against the 3 GB headroom | `ps` and Activity Monitor in phase 1, with the model loaded |
| Does the SSRF allowlist `127.0.0.1/32` let the plugin call through? | the on-Mac check in phase 2 |
| Are Data Tables present on Community 2.45.x, and does the heartbeat's query run against them? | phase 2; the sub-workflow fallback otherwise |
| Does n8n's SSRF switch refuse a LAN address while allowing the plugin? | phase 2, both directions |
| Does the Message trigger accept two contact cards as senders? | phase 3, when the partner's card would be added; one stub per sender otherwise |
| What is Shortcuts' timeout on Get Contents of URL? | moot with Respond Immediately; confirmed by the first real text's timing |
| Does the plugin's loopback check see n8n's requests as `127.0.0.1`? | phase 2; `::1` is also accepted |
| Does n8n make any outbound connection with the plist's switches off? | `nettop` or `lsof -i` for a day after first start, phase 1; the pf anchor's counters afterwards |
| Does `sysadminctl`'s role account take `/var/n8n` as a writable home, and does `sudo -u _n8n n8n import:workflow` see the daemon's user folder? | phase 1 and phase 2 |
| Does the pf anchor's line survive a macOS point update, and does the loader restore it? | after the first update that lands; the loader is written to re-add it regardless |
