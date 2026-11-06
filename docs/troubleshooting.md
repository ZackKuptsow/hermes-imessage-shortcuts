# Troubleshooting

## The Format Date action

Step 7 of [the install](../README.md#install) sets the action's own format. Clicking the token opens a popover whose Date Format and Time Format read Medium and Short when it has no format of its own: leave them. Set ISO 8601 there and Format Date is handed text instead of a date, keeps only the day (`2026-11-02`, without the time or offset), and the listener refuses every text. To clear a format already set, delete the token and pick Current Date again. Left as added, the action is handed the message instead and writes nothing, which is refused the same way. Either shows as a 400 with "the field 'sent_at' is not a moment" in the gateway log, the refusal for any `sent_at` without its time and offset: an empty one would give every repeat of a text one key, and Hermes answers the repeat with an error.

## Running the tests

The two commands are under [Tests](../README.md#tests) in the README.

Run both from the repository's root. Keep the `./` in front of `tests`: it can only mean a
folder right here, so where there is none the command stops with an error and runs nothing.
Without it, `unittest` falls back to importing a package named `tests`, and under
`hermes --run-module` that is Hermes's own suite, run against your real home. The `./` cannot
help inside `~/.hermes/hermes-agent`, where that folder is Hermes's suite.

Do not run these with `HERMES_HOME` pointed at a temporary directory: Hermes treats it as a
new data root, builds a runtime there, and re-executes the run under an interpreter without
`gateway`, which skips every adapter test and reads as green. The tests set the home through
`hermes_constants.set_hermes_home_override` instead.
