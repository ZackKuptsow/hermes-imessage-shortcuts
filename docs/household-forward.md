# The optional forward tool

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

A text that is exactly one of the service's fixed phrases does not reach the model (smart-home
ADR 0037): `OTIS`, `STOP`, `help` or `help <topic>`, `list`, `todos`, `my todos`, `reminders`,
`digest`, `digest off`, `done`, `done <number>`, `done shopping`, and `remove reminder <number>`
or `cancel reminder <number>`, each in any case and with the full stop, `!` or `?` a phone adds. Once the
text has passed the listener's checks, the plugin posts it itself, with the message's own
`sent_at`, and the service's answer goes to the phone the same way, so the model's session holds
no record of it. If the forward cannot run (no URL file, no Keychain item), the text goes to the
gateway as before. If it runs and fails, the phone gets a fixed line that holds no household
data and the log gets one line with no text. Every other text reaches the model, which may still
call the tool. `fixed_phrases.py` is the plugin's copy of the list, pinned by
`tests/test_fixed_phrases.py` to the ADR: a phrase the service adds reaches the model until it is
added there.
