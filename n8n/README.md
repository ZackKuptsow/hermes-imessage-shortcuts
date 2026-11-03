# n8n on the Mac, as its own user

n8n 2.x from npm, pinned, kept running by a LaunchDaemon as the role account `_n8n`,
reachable only at `http://127.0.0.1:5678`, with every switch that would send something off
the Mac turned off. It is the instance the texting relay will run on; nothing is imported
into it here.

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
