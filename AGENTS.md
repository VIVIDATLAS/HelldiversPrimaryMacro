# Project guide

This is a Python 3.11+, Windows 11 Pro-only, standard-library project. Windows
APIs are called only through `ctypes`. `main.py` is the CLI; configuration lives
in `config.toml`. `input_hooks.py` owns the low-level hook/message-loop thread,
`foreground.py` owns read-only foreground checks, `state_machine.py` owns all
generation-gated control transitions, `macro_engine.py` owns the active macro
worker, deferred Shift/RMB transactions, and short-lived canceled preparation cleanup, and
`input_backend.py` owns marked `SendInput` events, generated-input cleanup, and
the nonblocking hook/controller cleanup gate. `state_machine.py` tracks separate
`UNKNOWN`/`FULL` magazine states and owns macro, reload-preparation,
deferred-bypass, deferred RMB-off, and Shift transitions. Audio notifications
have their own queue/thread.
`simulation.py` provides deterministic end-to-end sessions using only fake OS
boundaries, input, clock/waiting, workers, and audio.
F23 runs the four-target stratagem macro and F24 runs Resupply followed by
Reinforce. Both are certain-foreground, disabled-idle-only controller workers
using tagged Left Ctrl, extended arrow scan codes, and a separately token-owned
MB1 action click. RMB, Shift, foreground loss/uncertainty, shutdown, and input
failure cancel and release only stratagem-owned input. G-keys may be externally
mapped to ordinary F23/F24; Python has no G Hub or Lua runtime dependency.
`cadence_diagnostics.py` provides bounded opt-in one-cycle PRIMARY correlation
across the worker, `SendInput`, and hooks. In automatic mode it freezes after
one configured MB1 hold pair and the first R pair, measures backend dispatch
independently of hook visibility, and
groups missing observations by device/action. It also retains bounded configured
fire/R flags and marker metadata, never cursor or physical
input. It is
disabled unless `--live --cadence-diagnostics` is explicitly supplied,
performs no hook-side I/O or waiting, and prints only after shutdown.
`windows_abi.py` owns the canonical unsigned pointer-width `ULONG_PTR` used by
both generated input and low-level hook structures. Live mode alone holds a
reference-counted 1 ms `timeBeginPeriod` lease from before component startup
until all live cleanup completes; all exit and partial-start paths attempt the
matching `timeEndPeriod`. Failure is reported once and falls back to the
default timer resolution. Relative interruptible shot waits remain unchanged;
there is no absolute-deadline rebasing or catch-up output.

PRIMARY is an automatic-hold rifle profile. Physical MB1 remains the macro
toggle, while active `[output]` uses ownership-tagged generated MB1. Helldivers
Fire stays bound to MB1 and the weapon must be placed in Automatic manually.
One natural cycle holds MB1 for `automatic_hold_ms = 4450`, releases it, sends
R immediately when `post_fire_reload_delay_ms = 0`, presses R for 25 ms, waits
2,000 ms, and completes at 6,475 ms. The tactical strategy assumes a manual
46-round start and expects the game's automatic cadence to consume 45 rounds
before the 46th; ammunition and muzzle events are not observable, so calibrate
only `primary.automatic_hold_ms` after manual live observation.

SECONDARY is tap mode: 13 shots at a 120 ms period with generated MB1 down for
35 ms and an 85 ms release interval only between consecutive shots. Its final
MB1-up and R-down
are consecutive with no post-shot wait; its configured complete cycle is
3,500 ms. Macro activation requires confirmed foreground ownership and known
`AIM_ON`; unmodified MB1 is suppressed but rejected without output or audio
from `AIM_OFF`/`UNKNOWN`. Idle physical MB2 passes through while the controller
tracks only an assumed toggle-aim state. During published firing, physical MB2
is pair-latched and deferred so firing cleanup and owned fire-up complete before
one tagged MB2 pair turns aim off. That transaction never emits Shift or R.
The controller invariant is `enabled or firing -> AIM_ON`; any observed or
committed departure from `AIM_ON` disables current firing.
Initial non-target foreground observations preserve `AIM_OFF` until the target
has first been observed `ACTIVE_CERTAIN`. After genuine foreground loss, one
new physical foreground MB2-down explicitly resynchronizes `UNKNOWN` to
`AIM_ON`; tagged/generated MB2 cannot do so. Successful Shift replay from
`UNKNOWN` generates no MB2 and conservatively normalizes aim to `AIM_OFF`.
Foreground physical Shift pairs are pair-latched,
suppressed, and replayed once with the same Left/Right scan code. Active firing
is disabled and owned generated MB1 is released before conditional aim-off; a tagged MB2
pair is generated only from `AIM_ON`, and its up precedes replayed Shift-down.
`AIM_OFF` and `UNKNOWN` never generate MB2. Shift never initiates reload work.
An already-active reload may finish while sprinting. When
`controls.shift_cancels_aim_natively` is true, Shift generates no MB2 and
records assumed aim OFF only after successful Shift replay. Persistent sprint
remains game-owned; later physical MB2 pairs never generate Shift.

Never read or depend on Lua, Logitech G HUB, AutoHotkey, drivers, game memory,
injection, interception, hardware emulation, anti-cheat workarounds, network
traffic, third-party packages, or administrator access. Only `--live` may
install hooks, suppress paired physical MB1/foreground Shift/firing-RMB events, or call
`SendInput`.

Authorized validation commands:

```powershell
python -m compileall .
python -m unittest discover -s tests -v
python main.py --check-config
python main.py --dry-run-primary-cycle
python main.py --dry-run-secondary-cycle
python main.py --dry-run-stratagem-four
python main.py --dry-run-stratagem-support
python main.py --simulate-session
git diff --check
git status --short
```

Do not run `--test-audio` or `--live` during automated development or tests.
Definition of done: configuration validates before hooks; target ownership is
fresh and certain for suppression/execution; Ctrl state is updated synchronously
in the keyboard hook; every MB1 pair latches one explicit decision; deferred
bypass output follows generated fire-up; injected events cannot control the
macro; at most one macro worker; `FULL` is set only by verified reload completion;
same-mode duplicate selections preserve the active generation; background
preparation ends armed/FULL or idle/UNKNOWN unless immediate MB1 invalidates it;
immediate fire never waits for preparation; ON/OFF transitions are deduplicated; audio test shutdown drains
accepted tones and exposes worker failures; all owned fire/MB1-bypass/MB2/Shift/R downs are released
on every exit; physical MB1-down is the only toggle edge and physical MB1-up is cleanup-only;
same-mode selection cannot mutate macro state; idle MB2 only updates inferred
aim, while firing MB2 disables before aim-off replay and never emits Shift/R;
Ctrl cannot restart canceled work; foreground regain requires neutral physical
input and never auto-restarts; foreground physical Shift is deferred once per pair, disables
active firing once, conditionally exits aim before same-scan Shift replay,
preserves an existing reload, never initiates `R`, and never restarts firing;
scenarios A-AZ and all authorized validation commands pass.
Ctrl-bypass and state-trace diagnostics default off and never log unrelated
input; trace reasons are transition-local and include elapsed milliseconds.
