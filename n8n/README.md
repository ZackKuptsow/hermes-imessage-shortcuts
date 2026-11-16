# n8n on the Mac, as its own user

n8n 2.x from npm, pinned, kept running by a LaunchDaemon as the role account `_n8n`,
reachable only at `http://127.0.0.1:5678`, with every switch that would send something off
the Mac turned off. It is the instance the texting relay runs on, from a workflow kept in git
as a template.

Why its own user: the relay never sends Apple Events, so n8n needs no login session, and as
`_n8n` it cannot read your home, the Hermes profiles, the login Keychain or the handle files.
Why npm and not Docker: Docker Desktop's VM would take an unmeasured share of the Mac's
memory headroom, and nothing here runs a shell command, so the workflows move to n8n 3.x in
Docker unchanged when the 2.x window closes.

## What you need

- The Mac Studio this was written for: `install.sh` refuses on any other machine.
- Homebrew's `node`, at `/opt/homebrew/bin/node`, version 24 or later (n8n 2.45 asks for
  `>=24.0.0`; written against 26.10.0). A `brew upgrade node` restarts nothing by itself,
  but the next start of n8n runs on the new Node.
- About 3 GB of headroom on the author's Mac with the model loaded. n8n takes a sixth of it:
  measured there on 2026-11-03 with the model loaded, n8n's two processes (`n8n start` and
  `n8n task-runner` in `ps -o rss=,comm= -u _n8n`) held about 480 MB resident just after
  the editor was used, a footprint of about 395 MB (Activity Monitor's Memory column, `top`'s
  `MEM`), and about 210 MB resident after three idle hours, once macOS had compressed the rest.

## Install

Three runs of one script and a watch beside it. The order matters: downloading n8n is
`_n8n`'s one outbound connection, and the watch has to be running for n8n's first start,
which is when it would first call home.

1. `sudo n8n/install.sh` makes the `_n8n` group and user (GID and UID 450, no password,
   home `/var/n8n`), checks that `_n8n` can neither list nor enter your home, makes
   `/var/n8n`, and installs the pinned n8n as `_n8n`. It writes the LaunchDaemon, then stops
   with exit 4 before starting it, saying the egress watch is not running.
2. In a terminal of its own: `sudo n8n/watch-egress.sh`. Leave it a day.
3. `sudo n8n/install.sh` again starts the daemon, waits for n8n's health check, and disables
   and stops it if anything of `_n8n` listens beyond `127.0.0.1`.
4. `sudo n8n/install.sh` a third time says `nothing to do`. Any later run that finds
   everything in place says the same and changes nothing.
5. **The owner account**, once, in a browser on the Mac at `http://127.0.0.1:5678`, before
   any workflow is imported: your own email address, and a password kept in your password
   manager, not the browser's. Nothing here records either. Skip any offer of free paid
   features: it sends your email address to n8n.

`install.sh` stops at the first thing it finds that is not its own, and changes nothing of it:
another user on port 5678 or 5679 (checked before anything is made), a `_n8n` whose record
differs (another UID, shell or home, or a password), UID 450 held by another name, `/var/n8n`
or a directory in it owned by someone else, or a log file that is a link, has a second name,
or is not `_n8n`'s. It also refuses to go on while `_n8n` can list or enter your home. Run as
root, it reads `_n8n`'s files as data and never runs one, since `_n8n` could have rewritten
any of them.

A run that stops partway, after writing a new plist or installing a new n8n but before
telling launchd, leaves a marker in `/var/run`, and the next run does the reload or restart
it left. It never says `nothing to do` while one is pending.

The account is made with `dscl`, the way macOS makes its own daemon accounts, not with
`sysadminctl -roleAccount`: that ignores `-home` (it sets `/var/empty`), gives the account a
password, and makes one macOS counts in the `staff` group, which can enter a home at its
default mode, 750. Its exit codes: 3 refused, 4 waiting for the watch, 5 n8n did not
answer its health check (read the logs below).

| Path | What it is |
|---|---|
| `/Library/LaunchDaemons/com.zack.hermes-imessage.n8n.plist` | the daemon, copied from `n8n/launchd/` unchanged; readable by every user, so it holds no secret |
| `/var/n8n` | `_n8n`'s home, `_n8n:admin`, mode 750 |
| `/var/n8n/.n8n` | n8n's data: the SQLite database, `config` with the encryption key; mode 700, `_n8n` and root alone |
| `/var/n8n/npm` | the pinned n8n |
| `/var/n8n/log` | `n8n.log`, `launchd.out.log`, `launchd.err.log`, group `admin`, mode 640 |

## Checks

Each is a gate from the design, broken or read the way it fails.

- **Listens on the loopback address alone**, for n8n and its task broker alike:
  `sudo lsof -nP -iTCP -sTCP:LISTEN -a -u _n8n` shows `127.0.0.1:5678` and `127.0.0.1:5679`
  and nothing else; `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5678/healthz`
  prints `200`. When a run of `install.sh` finds a listener beyond the loopback address, even
  from an n8n that never answered its health check, it disables the daemon before stopping it,
  so a restart of the Mac cannot start it again; `launchctl print-disabled system` lists
  it as disabled until a later run starts it.
- **Reads nothing of yours.** Each of these fails:
  `sudo -u _n8n ls /Users/zack`,
  `sudo -u _n8n cat ~/.hermes/profiles/imessage/data/imessage/zack-handles.txt`,
  `sudo -u _n8n cat ~/.hermes/profiles/imessage/.env`, and
  `sudo -u _n8n security find-generic-password -s household-assistant-imessage-secret -w`.
- **Logs you can read without sudo:** `ls -l /var/n8n/log` shows group `admin`, mode
  `-rw-r-----`, and `tail /var/n8n/log/n8n.log` works.
- **Sends nothing off the Mac.** The watch writes `~/n8n-egress-watch.log`: a sample of
  `lsof -nP -i -a -u _n8n` every 2 seconds, written when it changes. Stop it with Ctrl-C after
  a day that includes the owner account's creation; its last line counts the samples. This
  prints every socket line with an end beyond the loopback address, and should print nothing:

  ```sh
  awk '$3 == "_n8n" { n = split($9, ends, "->"); for (i = 1; i <= n; i++) { address = ends[i]; sub(/:[^:]*$/, "", address); if (address != "127.0.0.1" && address != "[::1]") { print; next } } }' ~/n8n-egress-watch.log
  ```

  Nothing printed means something only if there was something to read:
  `awk '$3 == "_n8n"' ~/n8n-egress-watch.log | wc -l` counts the socket lines it examined,
  which is never 0 while n8n runs. A sample every 2 seconds can miss a connection that opens
  and closes between two; the packet-filter anchor's counters are the lasting proof.

## The one file to back up

`/var/n8n/.n8n/config` holds the key that decrypts every credential n8n stores. Without it
the credentials are lost, though the workflows are not. `sudo cp` it somewhere only you can
read.

## The relay

`workflows/imessage-relay.json` is the switchboard: a webhook behind a header secret, the sender
looked up in a routing table, the text forwarded to the plugin unchanged, and one row of metadata
written to a Data Table. In git it is a template, with the handles and the table's id taken out as
tokens; `render.py` fills them on the Mac from the plugin's own handle file, so the routing table
and the allow list cannot disagree, and `export.py` takes them out again. `n8n-cli.sh` runs any n8n
command as the daemon would see it. Run all of these as yourself from the repository's root: they
ask for `sudo` when they need it. Each step below happens once, except where it says otherwise.

**1. Two credentials**, in the editor, each of type *Header Auth*. Their names are exact, since the
template refers to them by name and holds neither secret.

| Credential name | Header name | Value |
|---|---|---|
| `Shortcuts relay secret` | `X-Relay-Secret` | a new secret from `openssl rand -hex 32`, kept in your password manager; the Shortcut will send it |
| `Hermes iMessage plugin secret` | `X-Hermes-Secret` | a copy of `IMESSAGE_SHORTCUTS_SECRET` from `~/.hermes/profiles/imessage/.env` |

Set each one's **Allowed HTTP Request Domains** too. n8n applies it to the HTTP Request and GraphQL
nodes alone, so `Shortcuts relay secret`, which only the Webhook uses to check what arrives, takes
`None`, and n8n can never send it anywhere. `Hermes iMessage plugin secret` takes `Specific Domains`
with `127.0.0.1`: n8n compares the host and not the port.

**2. The `run_record` Data Table**, in the editor, since n8n's CLI imports workflows and not
tables. Name it `run_record`, with these five columns. n8n gives every table `id`, `createdAt` and
`updatedAt` of its own, which cannot be removed and hold no text:

| Column | Type |
|---|---|
| `received_at` | String |
| `route` | String |
| `outcome` | String |
| `plugin_status` | Number |
| `duration_ms` | Number |

Its id is the 16 letters and digits at the end of its address in the browser, after `/datatables/`.
The name `run_record` is not it, and `render.py` and `export.py` refuse anything else. Keep it for
the steps below; it is not a secret, but it stays out of git.

**3. The workflow**, imported, then opened once in the editor:

```sh
n8n/import.sh TABLE_ID
```

`import.sh` renders the template with your handles and the table's id into a folder `_n8n` can
read, imports it, and removes every rendered copy whether or not the import worked. It refuses while
a workflow of that name exists, since two copies would hold one webhook path between them. Open
`iMessage relay` and check each node against `workflows/building-the-relay.md`, which says what
each is set to and why. n8n 2.45 saves on its own as you edit, so there is no Save to press, and an
untouched workflow is not written again. The workflow arrives unpublished; leave it so until step 5.

**4. Export it into git.** The first command finds its id. `_n8n` writes the export, and it is
readable by you through the admin group. Afterwards `git diff` shows what changed in the editor:

```sh
n8n/n8n-cli.sh list:workflow
n8n/n8n-cli.sh export:workflow --id=WORKFLOW_ID --output=/var/n8n/relay-export.json
python3 n8n/export.py ~/.hermes/profiles/imessage/data/imessage/zack-handles.txt \
  /var/n8n/relay-export.json n8n/workflows/imessage-relay.json --run-record-table-id TABLE_ID
sudo -u _n8n rm /var/n8n/relay-export.json
python3 -m unittest discover -s ./tests -p 'test_n8n_workflows.py'
```

`export.py` refuses, writing nothing, if anything in the result still looks like a handle.

**5. Publish it** in the editor: its webhook answers nothing until then.

When the handle file changes, the relay's table is out of date until you delete the workflow in the
editor, run `n8n/import.sh TABLE_ID` again and publish.

**6. Read a run back.** This posts one real text through n8n to the plugin, so Hermes answers it.
`read -rs` takes the relay secret without showing it, the handle comes from your own file, and both
reach `curl` through its standard input, since a command line can be read by anyone on the Mac:

```sh
read -rs RELAY_SECRET
SENDER="$(grep -v '^#' ~/.hermes/profiles/imessage/data/imessage/zack-handles.txt | head -1)"
SENT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'header = "X-Relay-Secret: %s"\ndata = "{\\"sender\\":\\"%s\\",\\"sent_at\\":\\"%s\\",\\"text\\":\\"Reply with one word: relay.\\"}"\n' \
  "$RELAY_SECRET" "$SENDER" "$SENT_AT" \
  | curl -K - -sS -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' http://127.0.0.1:5678/webhook/imessage-relay
sudo -u _n8n sqlite3 -readonly /var/n8n/.n8n/database.sqlite \
  'SELECT received_at, route, outcome, plugin_status, duration_ms FROM data_table_user_TABLE_ID;'
```

The `curl` prints `200`, since the webhook answers before anything else runs. The row should read
route `zack`, outcome `accepted`, status `202.0` (Data Table numbers are stored as reals), and the
reply should reach your phone.

**The outcomes**, a closed list: `accepted` (the plugin's 202), `dropped` (not in the routing table,
so nothing was posted), `dropped_by_plugin` (its 204), `secret_refused` (its 401), `plugin_refusing`
(any other status), `plugin_unreachable` (the connection failed), `timeout` (5 seconds ran out). The
plugin's real status is in `plugin_status`, and there is none for the last two or for `dropped`.

## The check

`check_relay.py` proves the relay end to end at the Mac, by breaking its gates and reading what each
post leaves. It imports the template under its own name, webhook path and credentials, aimed at a
stand-in plugin on a free port, and writes to a table of its own. So it never posts to the real
plugin on 8645, never changes the real relay or reads its secrets, and never reads your handle file:
its senders are made up, and its secrets are random for each run. It removes what it made when it
ends, and if a run is cut short, the next one removes the leftovers first. It prints no secret,
handle or body.

Once, in the editor, make a second Data Table with the same five columns as `run_record`, named
`run_record_check`, with the same types: three String, then `plugin_status` and `duration_ms`
Number. The check finds it by name, and refuses to start, naming each difference, if its columns
are not those five. Then, from the repository's root:

```sh
python3 n8n/check_relay.py
```

It does not restart n8n, so the real relay keeps serving through a run. It publishes through the CLI,
and the running daemon picks that up within about half a minute. Removing its workflow takes as long
again: n8n refuses to delete a workflow until the daemon has finished unpublishing it.

Each case prints its status, the rows it found, the log lines of its alarm, and `pass` or `FAIL`, and
it exits 0 only if all of them held:

| Case | Status | Row, and the rest | Alarm (Issue #10) |
|---|---|---|---|
| no secret, wrong secret | 403 | no row, and nothing reaches the stand-in | none |
| unknown sender | 200 | `dropped`, and nothing reaches the stand-in | none |
| plugin unreachable (the stand-in stopped) | 200 | `plugin_unreachable` | `plugin_unreachable` |
| accepted | 200 | `accepted`, 202, with the stand-in holding the body posted, unchanged | none |
| dropped by the plugin | 200 | `dropped_by_plugin` 204 | none |
| refused secret | 200 | `secret_refused` 401 | `secret_refused` |
| plugin refusing, with a 503, a 400 and a 403 | 200 | `plugin_refusing` and the status | `plugin_refusing` |
| timeout (the stand-in answers after 7 seconds) | 200 | `timeout` | `timeout` |
| garbage from the plugin (an answer that is not HTTP) | 200 | `plugin_unreachable` | `plugin_unreachable` |
| a body with no sender (`null`) | 200 | `dropped` | none |
| table gone (posted to a third copy of the relay, whose insert names a table that is not there) | 200 | no row: its run throws at the insert | `relay_failed` |

An alarm holds when there is one row on route `alarm` naming it and at least one line in `n8n.log`
saying `alarm <outcome>`; a case with no alarm holds only with no alarm row. The check's relay names
the check's own `Relay failed check` as its error workflow, so the real `Relay failed` never hears
of a check, and the check waits for n8n to publish it before the first post. No post can make the
relay's run throw, since every expression in it copes with any JSON (a `null` body was posted at the
Mac on 2026-11-16 and was dropped), so the catch-all `relay_failed` is seen through the table-gone
copy, which fails as the relay would with `run_record` gone. Each alarm case reports 2 log lines:
n8n's error reporter writes every node failure twice, the relay's own failures too.

A last post of over 65,536 bytes is reported rather than judged, to show what the webhook does with
a body past the plugin's limit.

To see a gate fail, break it on purpose. Each of these should go red and say it was seen to fail, and
each exits 3 if nothing noticed:

```sh
python3 n8n/check_relay.py --break wrong-header
python3 n8n/check_relay.py --break ip-allowlist
python3 n8n/check_relay.py --break routing-table
```

Measured on the Mac on 2026-11-16, against n8n 2.45.7, with Issue #10's alarms: all 14 cases held and
the run exited 0, the timeout at 5,023 ms. Every alarm case found its one alarm row and 2 log lines;
`accepted`, `dropped`, `dropped_by_plugin` and the body with no sender raised none; garbage from the
plugin came out `plugin_unreachable` and alarmed; the table-gone copy wrote no row and alarmed
`relay_failed`. The over-long post now alarms `plugin_refusing` too, as its 400 should. The three
breaks each went red and exited 1: with the wrong header and with the IP allowlist, 2 of 14 held, every
post carrying the secret answered 403 with no row, no alarm and nothing forwarded; with the routing
table, 3 of 14 held, every route and outcome came out the other way and no alarm was raised but the
table-gone copy's, which still forwarded to the stand-in and so failed. An earlier run that morning
held 12 of 13: a `null` body, expected to make the relay throw, was dropped instead, which is why the
catch-all is now seen through the table-gone copy.

Measured on the Mac on 2026-11-09, against n8n 2.45.7: all nine cases held and the run exited 0, with
the timeout at 5,040 ms. The post of 70,085 bytes was answered 200, reached the stand-in as 70,080
bytes, and wrote `plugin_refusing` 400, so the webhook passes a body past the plugin's limit on, and
the plugin's own limit is what refuses it. Each break went red for its own reason: with the wrong
header and with the IP allowlist, every post carrying the secret was answered 403, wrote no row and
reached nothing; with the routing table, every route and outcome came out the other way. Those three
runs were made while the check's table still had `duration_ms` as a String column, which by itself
failed seven cases of a run with nothing broken. The routing-table run's lines name that failure too,
but the wrong route and outcome they also name do not depend on it, and the other two wrote no row.

## The alarms

Three workflows, each a template in `workflows/`:

| Workflow | Template | Alarms on |
|---|---|---|
| `iMessage relay` | `imessage-relay.json` | nothing itself. After its run record, a run ending `plugin_unreachable`, `plugin_refusing` (any status but 202, 204 and 401), `secret_refused` or `timeout` fails on purpose, with the outcome's name as its message, so n8n hands it to `Relay failed`. `accepted`, `dropped` and `dropped_by_plugin` end as successes |
| `Relay failed` | `relay-failed.json` | each relay run that fails. The outcome is the failed run's message when that is exactly one of the four names above, and `relay_failed` for any other failure |
| `Inbound heartbeat` | `inbound-heartbeat.json` | run every morning at 08:00, no `accepted` run from route `zack` in the last 36 hours: outcome `inbound_quiet`. The same run deletes the rows older than 7 days, and no others |

An alarm is two things. One is a row in `run_record` with route `alarm` and an outcome that names
its cause, its `received_at` the moment it was raised, and no `plugin_status` or `duration_ms`.
The outcome is a fixed name and never an error's message, since a message can carry what a node was
handed: `Relay failed` compares the message with the four names and stores only the match, and its
test refuses any other read of the failed run but its id. The other is a line in `n8n.log`, at
`error`, whose message is `alarm <outcome>`: each alarm ends in a Stop and Error node, the one way
n8n 2.45.7 lets a workflow write a line of its choosing, so the alarm workflow's own run ends as
failed too. n8n writes that line twice, as it does every node failure, and the failure that caused
the alarm, `timeout` say, twice more just before it. To read them:

```sh
sudo -u _n8n sqlite3 -readonly /var/n8n/.n8n/database.sqlite \
  "SELECT received_at, outcome FROM data_table_user_TABLE_ID WHERE route = 'alarm' ORDER BY id;"
grep '"message":"alarm ' /var/n8n/log/n8n.log
```

**Installing them.** `import.sh` takes the workflow as its second argument, `relay` by default:

```sh
n8n/import.sh TABLE_ID relay-failed
n8n/import.sh TABLE_ID inbound-heartbeat
```

Publish both in the editor, as the relay is: n8n hands a failed run only to an error workflow that
is published, and a Schedule Trigger fires only once published. The relay names `Relay failed` by
its fixed id, `relayFailedAlarm`, which the template carries and `export.py` keeps; every other
workflow id is dropped on export as before. Import `Relay failed` before the relay starts failing on
purpose, or those runs alarm nowhere. Export each back as the relay's step 4 does, with the id
`n8n-cli.sh list:workflow` gives; `git diff` then shows anything the editor changed.

Installed this way on the Mac on 2026-11-16: `Relay failed`, the relay and the heartbeat each exported
back byte-identical to its template, so n8n stores what the templates hold. Each was only opened,
not edited, so this does not show how the editor would write a node it changed.

**The two numbers to tune** are at the top of the heartbeat, in its `Heartbeat constants` node:
`quiet_hours`, 36, how long without an accepted run before it alarms, and `keep_days`, 7, how long a
row is kept. `tests/test_n8n_workflows.py` holds both values too, so a change is made in the two places.
The heartbeat asks by each row's own `createdAt`, which n8n stamps, rather than by `received_at`, a
string whose offset changes with daylight saving time.

**Trying the heartbeat by hand.** Cut-over has not happened, so `run_record` holds only test rows and
may be emptied (decided on Issue #10). Run it with the editor's Execute workflow, and read the table
after each run with:

```sh
sudo -u _n8n sqlite3 -readonly /var/n8n/.n8n/database.sqlite \
  "SELECT id, route, outcome, createdAt FROM data_table_user_TABLE_ID ORDER BY id;"
```

1. With `run_record` emptied in the editor: one `inbound_quiet` row, and its log line. The run stops
   red on `Log the alarm`, which is the alarm working.
2. With an `accepted` row on route `zack` added in the editor: no new row, and the run ends green.
3. With the oldest row backdated 8 days, which the editor cannot do, so it is done in SQLite; n8n
   keeps `createdAt` as UTC text in this form: the backdated row goes, the younger stays.

```sh
sudo -u _n8n sqlite3 -cmd ".timeout 5000" /var/n8n/.n8n/database.sqlite \
  "UPDATE data_table_user_TABLE_ID SET createdAt = strftime('%Y-%m-%d %H:%M:%f', 'now', '-8 days')
   WHERE id = (SELECT MIN(id) FROM data_table_user_TABLE_ID);"
```

Run on the Mac on 2026-11-16, as written: the first run wrote `inbound_quiet` at 18:43:31.180 UTC and
its 2 log lines 10 ms later; the second wrote nothing beside the accepted row; the third deleted the
alarm row, backdated to 2026-11-08, and kept the accepted row. No other `alarm inbound_quiet` line was
logged.

**What raises nothing yet.** A heartbeat that fails outright, its Data Table gone or n8n down at
08:00, raises no alarm: nothing watches the heartbeat until T08. Neither does a relay run n8n never
starts, which the heartbeat's 36 hours are there to catch.

What n8n 2.45.7 does that decides how these run, read in its installed source on 2026-11-15:

- It hands a failed run to the error workflow even though no run is saved (`hookFunctionsSave` in
  `execution-lifecycle-hooks.js`), but not a run started by hand in the editor, and only to an error
  workflow that is published (`loadErrorWorkflowData` in `workflow-execution.service.js`). So `Relay
  failed` is published like the relay, and is tried with a real post, not the editor's run button.
- It hands over only runs that fail. The relay's request continues on error and never errors on a
  status, so without failing on purpose its four failing outcomes would end as successes.
- A workflow names its error workflow by that workflow's id, in `settings.errorWorkflow`, not by name.
- The Schedule Trigger keeps the time of `GENERIC_TIMEZONE`, pinned in the plist to
  `America/New_York`, so the plist rather than the Mac's time zone setting decides when 08:00 is.
- No node on the closed list writes a line of its own to `n8n.log`, and n8n writes none for a run at
  `info`; a node that fails is logged at `error`, with its message, which is why Stop and Error is on
  the list.

`check_relay.py` sees every row of the table above but the heartbeat's: it imports its own `Relay
failed check` beside its relay, and each case says which alarm it raised and how many log lines.

## Upgrading

First copy the database: a new n8n migrates it on its first start, and n8n does not support
running an older version on a migrated database, so never lower `N8N_VERSION`; the copy is
the way back. SQLite's own backup is safe while n8n runs:

```sh
sudo sqlite3 /var/n8n/.n8n/database.sqlite ".backup '/Users/zack/n8n-database-before-upgrade.sqlite'"
```

Then change `N8N_VERSION` at the top of `install.sh` and run it again: it installs that
version over the old one and restarts the daemon. n8n 2.x is supported until about December
2027; before then the workflows move to n8n 3.x, which ships as a Docker image alone.

## Uninstalling

```sh
sudo launchctl bootout system/com.zack.hermes-imessage.n8n
sudo rm /Library/LaunchDaemons/com.zack.hermes-imessage.n8n.plist
```

`/var/n8n` and the `_n8n` user stay: `/var/n8n/.n8n` holds the database and the encryption
key, and running `install.sh` again picks both up where they were. Like the first start, that
start waits for the egress watch, and exits 4 until it runs.
