# Project guide

This is a Python 3.11+, Windows 11 Pro-only, standard-library project. Windows
APIs are called only through `ctypes`. `main.py` is the CLI; configuration lives
in `config.toml`. `input_hooks.py` owns the low-level hook/message-loop thread,
`foreground.py` owns read-only foreground checks, `state_machine.py` owns all
control transitions, `macro_engine.py` owns the sole optional worker, and
`input_backend.py` owns marked `SendInput` events, generated-input cleanup, and
the nonblocking hook/controller cleanup gate. `state_machine.py` tracks separate
`UNKNOWN`/`FULL` magazine states and owns macro, reload-preparation, and deferred
bypass transitions. Audio notifications have their own queue/thread.

Never read or depend on Lua, Logitech G HUB, AutoHotkey, drivers, game memory,
injection, interception, hardware emulation, anti-cheat workarounds, network
traffic, third-party packages, or administrator access. Only `--live` may
install hooks, suppress paired physical MB1 events, or call `SendInput`.

Authorized validation commands:

```powershell
python -m compileall .
python -m unittest discover -s tests -v
python main.py --check-config
python main.py --dry-run-primary-cycle
python main.py --dry-run-secondary-cycle
```

Do not run `--test-audio` or `--live` during automated development or tests.
Definition of done: configuration validates before hooks; target ownership is
fresh and certain for suppression/execution; Ctrl state is updated synchronously
in the keyboard hook; every MB1 pair latches one explicit decision; deferred
bypass output follows generated MB1-up; injected events cannot control the
macro; one worker maximum; `FULL` is set only by verified reload completion;
fire never precedes required preparation; ON/OFF transitions are deduplicated;
all owned MB1/R downs are released on every exit; the authorized validation
commands all pass. Ctrl-bypass diagnostics default off and never log unrelated
input.
