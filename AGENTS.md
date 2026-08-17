# Project guide

This is a Python 3.11+, Windows 11 Pro-only, standard-library project. Windows
APIs are called only through `ctypes`. `main.py` is the CLI; configuration lives
in `config.toml`. `input_hooks.py` owns the low-level hook/message-loop thread,
`foreground.py` owns read-only foreground checks, `state_machine.py` owns all
generation-gated control transitions, `macro_engine.py` owns the active macro
worker, deferred Shift transaction, and short-lived canceled preparation cleanup, and
`input_backend.py` owns marked `SendInput` events, generated-input cleanup, and
the nonblocking hook/controller cleanup gate. `state_machine.py` tracks separate
`UNKNOWN`/`FULL` magazine states and owns macro, reload-preparation, and
deferred-bypass transitions. Audio notifications have their own queue/thread.
`simulation.py` provides deterministic end-to-end sessions using only fake OS
boundaries, input, clock/waiting, workers, and audio.

PRIMARY is a configurable 45-shot semi-automatic rifle profile. Helldivers must
be configured to semi-automatic manually; the macro does not change firing
mode. Its current cadence is an 85 ms period with MB1 down for 35 ms and a
50 ms release interval only between consecutive shots. After shot 45,
final MB1-up and R-down are consecutive under one short I/O boundary with no
post-shot wait. The tactical-reload strategy
assumes the user manually starts with 46 rounds, fires 45, leaves one chambered,
and reloads back to 46. Immediate first activation can begin from an unknown or
partial magazine because ammunition, ignored inputs, empty reserves, and
interrupted reloads are not observable. During later rate calibration, change
only `primary.shot_period_ms` in `config.toml`.

SECONDARY is 13 shots at a 120 ms period with MB1 down for 35 ms and an 85 ms
release interval only between consecutive shots. Its final MB1-up and R-down
are consecutive with no post-shot wait; its configured complete cycle is
3,500 ms. Physical MB2 always passes through while the controller tracks only
an assumed toggle-aim state. Foreground physical Shift pairs are pair-latched,
suppressed, and replayed once with the same Left/Right scan code. Active firing
is disabled and owned MB1 is released before conditional aim-off; a tagged MB2
pair is generated only from `AIM_ON`, and its up precedes replayed Shift-down.
`AIM_OFF` and `UNKNOWN` never generate MB2. Shift never initiates reload work.
An already-active reload may finish while sprinting. When
`controls.shift_cancels_aim_natively` is true, Shift generates no MB2 and
records assumed aim OFF only after successful Shift replay. Persistent sprint
remains game-owned; later physical MB2 pairs never generate Shift.

Never read or depend on Lua, Logitech G HUB, AutoHotkey, drivers, game memory,
injection, interception, hardware emulation, anti-cheat workarounds, network
traffic, third-party packages, or administrator access. Only `--live` may
install hooks, suppress paired physical MB1/foreground Shift events, or call
`SendInput`.

Authorized validation commands:

```powershell
python -m compileall .
python -m unittest discover -s tests -v
python main.py --check-config
python main.py --dry-run-primary-cycle
python main.py --dry-run-secondary-cycle
python main.py --simulate-session
git diff --check
git status --short
```

Do not run `--test-audio` or `--live` during automated development or tests.
Definition of done: configuration validates before hooks; target ownership is
fresh and certain for suppression/execution; Ctrl state is updated synchronously
in the keyboard hook; every MB1 pair latches one explicit decision; deferred
bypass output follows generated MB1-up; injected events cannot control the
macro; at most one macro worker; `FULL` is set only by verified reload completion;
same-mode duplicate selections preserve the active generation; background
preparation ends armed/FULL or idle/UNKNOWN unless immediate MB1 invalidates it;
immediate fire never waits for preparation; ON/OFF transitions are deduplicated; audio test shutdown drains
accepted tones and exposes worker failures; all owned MB1/MB2/Shift/R downs are released
on every exit; MB1-down is the only toggle edge and MB1-up is cleanup-only;
same-mode selection and MB2 cannot mutate macro state; Ctrl cannot restart
canceled work; foreground regain requires neutral physical input and never
auto-restarts; foreground physical Shift is deferred once per pair, disables
active firing once, conditionally exits aim before same-scan Shift replay,
preserves an existing reload, never initiates `R`, and never restarts firing;
scenarios A-AJ and all authorized validation commands pass.
Ctrl-bypass and state-trace diagnostics default off and never log unrelated
input; trace reasons are transition-local and include elapsed milliseconds.
