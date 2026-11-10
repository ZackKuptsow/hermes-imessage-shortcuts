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

**3. The workflow**, imported, then opened and saved once in the editor:

```sh
n8n/import.sh TABLE_ID
```

`import.sh` renders the template with your handles and the table's id into a folder `_n8n` can
read, imports it, and removes every rendered copy whether or not the import worked. It refuses while
a workflow of that name exists, since two copies would hold one webhook path between them. Open
`iMessage relay`, check each node against `workflows/building-the-relay.md`, which says what each
is set to and why, and save: saving makes the editor write each node in its own form, which the
export then carries into git. The workflow arrives unpublished; leave it so until step 5.

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

Each case prints its status, the row it found, and `pass` or `FAIL`, and it exits 0 only if all of
them held:

| Case | Status | Row, and the rest |
|---|---|---|
| no secret, wrong secret | 403 | no row, and nothing reaches the stand-in |
| unknown sender | 200 | `dropped`, and nothing reaches the stand-in |
| plugin unreachable (the stand-in stopped) | 200 | `plugin_unreachable` |
| accepted | 200 | `accepted`, 202, with the stand-in holding the body posted, unchanged |
| dropped by the plugin, refused secret, plugin refusing | 200 | `dropped_by_plugin` 204, `secret_refused` 401, `plugin_refusing` 503 |
| timeout (the stand-in answers after 7 seconds) | 200 | `timeout` |

A last post of over 65,536 bytes is reported rather than judged, to show what the webhook does with
a body past the plugin's limit.

To see a gate fail, break it on purpose. Each of these should go red and say it was seen to fail, and
each exits 3 if nothing noticed:

```sh
python3 n8n/check_relay.py --break wrong-header
python3 n8n/check_relay.py --break ip-allowlist
python3 n8n/check_relay.py --break routing-table
```

Measured on the Mac on 2026-11-09, against n8n 2.45.7: all nine cases held and the run exited 0, with
the timeout at 5,040 ms. The post of 70,085 bytes was answered 200, reached the stand-in as 70,080
bytes, and wrote `plugin_refusing` 400, so the webhook passes a body past the plugin's limit on, and
the plugin's own limit is what refuses it. Each break went red for its own reason: with the wrong
header and with the IP allowlist, every post carrying the secret was answered 403, wrote no row and
reached nothing; with the routing table, every route and outcome came out the other way. Those three
runs were made while the check's table still had `duration_ms` as a String column, which by itself
failed seven cases of a run with nothing broken. The routing-table run's lines name that failure too,
but the wrong route and outcome they also name do not depend on it, and the other two wrote no row.

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
