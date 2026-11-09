# Security model

How the plugin decides who gets in and what a text can do. [The README](../README.md) has the summary.

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

Every status code, and what a dropped request says, is in [http-api.md](http-api.md).

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
