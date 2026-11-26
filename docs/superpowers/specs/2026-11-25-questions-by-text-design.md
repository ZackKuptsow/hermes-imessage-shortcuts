# A session's question reaches Zack's phone

- Date: 2026-11-25
- Status: draft, for Zack's approval. Step 1 below is built on this branch; step 2 is not
  designed.
- Sources: Hermes Agent's installed source at v0.23.1 (commit 3b42157858) and its docs as
  published on 2026-11-25; the plugin catalog, searched the same day; this repository's
  README, `docs/security.md` and code at `aa51062`; smart-home ADRs 0034 and 0035 as this
  repository's code cites them (the records themselves were not reread for this draft).

## Intent and the why

Zack leaves a coding session running on the Mac Studio and walks away. When the session
stops to ask him something it waits, for as long as he is gone (`agent.clarify_timeout` is
`0` on his Mac). He asked whether such a session could reach him "through the same iMessage
when it has questions", the way Claude Code's remote control lets him steer a session from
his phone, and to build it here, in the public repository, with every value behind an
environment variable.

Nothing in Hermes or its catalog does this for a Mac's own Messages account. The catalog's
notifiers (`signal-notify`, `telegram-notify`, `pushover-hermes-plugin`) are for other
channels, and its iMessage plugins all carry texts to an agent, as this one does. Hermes's
own `/handoff` moves a whole session to a platform enabled in the session's own profile,
which this plugin refuses to be.

## Two steps, one built

1. **The question goes out.** Built here. A question nobody answers at the Mac within a
   delay is texted to him. He still answers at the Mac.
2. **The answer comes back by text.** Not designed, and not started. It would let a text
   steer a session that has a shell, which this plugin's README and smart-home ADR 0035
   promise never happens, and it would send that text to a cloud model. It waits on three
   things: a decision record in smart-home that supersedes or amends ADR 0035; Hermes
   v0.23.4 or later on the Mac, whose `on_session_prompt` hook carries the identifier an
   answer needs (the Mac runs v0.23.1); and a spike proving how a plugin in the texting
   profile hands an answer to a session another profile owns through a public interface.

## Step 1 as built

Two pieces, in two profiles, joined by one HTTP request on the loopback address.

**`imessage-questions/`, a second plugin, in the profile whose sessions ask.** It has to be
separate: Hermes runs a plugin's hooks only in the profile that owns the session, and
`imessage-shortcuts` refuses to start in any profile but `imessage`.

- It registers Hermes's two observer hooks, `on_human_input_request` and
  `on_human_input_resolved`. The first starts a timer for a `clarify` question; the second
  cancels it. Any other kind of prompt (an approval, a sudo password) is ignored.
- When the timer fires it posts `{"question", "session"}` to `/imessage/question` with the
  question secret. `session` is the name of the session's workspace folder, or the surface
  it runs on when it has none.
- The secret is `IMESSAGE_QUESTIONS_SECRET` in that profile's `.env`. The settings are
  `delay_seconds` (120), `host` (`127.0.0.1`) and `port` (8645) under
  `plugins.entries.imessage-questions.settings` in its `config.yaml`. They are read when
  each question is asked, on the hook's own thread, which carries the profile's scope.
- The post goes through no proxy and follows no redirect, and the settings refuse a host
  off the loopback address, so the secret is sent nowhere but the listener.

**`POST /imessage/question`, a third route on the listener, in the texting profile.**

- It exists only while `IMESSAGE_SHORTCUTS_QUESTION_SECRET` is set, and takes that secret
  alone. The listener refuses to start while it equals the Shortcut's secret.
- It composes the text (a line naming the session, the question, a line saying to answer at
  the Mac) and sends it to the first handle on the allow list through `_send_text`, the
  adapter's one way out. It opens no session, so the local model sees none of it.

## Calls this draft made that Zack has not ruled on

- **A delay, not a switch.** A question is texted after two minutes unanswered, in every
  session of the profile. The alternative is a command that turns it on per session.
- **A secret of its own for the route**, in place of sharing the Shortcut's. The asking
  profile's copy can then do one thing.
- **The route names no recipient.** The first handle on the allow list gets every question,
  an email address included, where the household forward uses the first phone number.
- **Only questions.** An approval's command is not texted, even as a notice.
- **The text says to answer at the Mac.** A reply to it otherwise reaches the texting
  assistant, which knows nothing of the question.
- **The names**: `imessage-questions`, `IMESSAGE_QUESTIONS_SECRET`,
  `IMESSAGE_SHORTCUTS_QUESTION_SECRET`, `/imessage/question`.

## What is proven, and what is not

Proven in this repository's suites, under Hermes's interpreter and a plain `python3`:

- 59 new tests, 472 in all. One drives a question through both plugins over real HTTP to
  the stand-in imsg; another shows a question answered first sends nothing.
- 41 single-line breaks of the new code, each of which turned at least one of those tests
  red.
- `hermes plugins validate` passes for both plugin directories.

Not proven, because each needs the live gateway:

- That Hermes v0.23.1 fires the two hooks for a question asked in a CLI session the gateway
  hosts. Read in `tools/clarify_tool.py` and `tools/human_input_hooks.py`; not run.
- That `ctx.session_cwd()` gives the workspace folder inside the hook, and that the settings
  path above is where `hermes config set` writes. Read in `hermes_cli/plugin_session.py`
  and `hermes_cli/plugins_context.py`; not run.
- That a text sent this way arrives. The send is the same `imsg` call a reply makes.

The first live question answers all three. Until it has run, the README's install steps
for this are a sequence nobody has run from a clean state.

## Limits

- The choices of a multiple-choice question are not in the text: the hook Hermes v0.23.1
  fires carries the question alone.
- Two sessions in one folder carry the same label.
- With `plugins.isolation: host` the plugin runs outside the session's context, so the label
  is likely to fall back to the surface. Not tested.
- A question asked while the gateway restarts is lost with its timer; the session asks
  again or waits.

## What follows a yes

1. Merge this branch; CI runs both jobs.
2. An installer step in dotfiles (`hermes/mac-studio/setup.sh`) that writes the two secrets,
   links the plugin into the default profile and enables it, with its tests.
3. At the Mac: pull, run the installer, restart the gateway, ask one question and wait.
4. Step 2's decision record and spike, as their own work.
