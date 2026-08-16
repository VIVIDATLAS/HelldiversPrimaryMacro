# Helldivers primary/secondary macro (Windows 11 Pro)

This project is a visible, foreground-restricted macro using Python 3.11+
standard-library modules and ordinary Windows APIs through `ctypes`. It does
not use administrator privileges, game memory, screen recognition, DLL
injection, network inspection, hardware emulation, concealed automation, or an
anti-cheat bypass. Verify the game's current rules before use.

Ordinary `SendInput` can be rejected by the game, UIPI/integrity-level rules,
or anti-cheat software. A rejection stops work and releases owned inputs; this
project does not provide a bypass or elevated-mode workaround.

## Setup and safe inspection

1. Install 64-bit Python 3.11 or newer on Windows 11 Pro.
2. Open PowerShell in this directory. No packages need installation.
3. Review `config.toml`. The default target is `helldivers2.exe`.
4. Run `python main.py --check-config`.
5. Start the game yourself, make it foreground, and run
   `python main.py --identify-foreground --delay 5` to inspect the owning
   process without hooks, input, suppression, or sound.
6. Run `python main.py --simulate-session`. This exercises deterministic
   scenarios A through AC with fake hooks, input, foreground, time, workers,
   and audio only.
7. Review both dry runs before deliberately choosing `--live`.

Running `python main.py` without a mode prints help and does nothing.

## Primary semi-automatic profile and calibration

PRIMARY is a configurable 45-shot semi-automatic rifle profile. The weapon is
capable of automatic fire, so configure it to semi-automatic manually in
Helldivers before using this profile. The macro does not inspect or change the
weapon's firing mode; it emits 45 independent MB1 click pairs.

The initial primary configuration is:

```toml
[primary]
shots_per_cycle = 45
shot_period_ms = 85
fire_press_ms = 35
reload_press_ms = 25
reload_wait_ms = 2000
```

`shot_period_ms` is authoritative. The MB1-up interval is derived as
`shot_period_ms - fire_press_ms`, so the configured values produce 35 ms down
and a 50 ms release interval between consecutive shots. Timing remains entirely
configuration-driven; this correction does not change PRIMARY behavior.

For later calibration, change only `primary.shot_period_ms` in `config.toml`,
then run configuration validation and the primary dry run. Do not change the
shot count, click-down time, or reload timing during firing-rate calibration.
The configured period is an observed operational value, not an inferred weapon
limit. Do not change it during unrelated control or reload work.

The tactical-reload cycle assumes a maximum loaded state of 46: start at 46,
fire exactly 45 shots, leave one round chambered, reload, and return to 46. Do
not configure 46 shots per cycle because firing the chambered round would empty
the weapon and the next reload could return only 45.

The user should manually establish 46 rounds before starting the synchronized
cycle. If activation begins with only 45 rounds, the first cycle may empty the
weapon and its reload may return only 45. Immediate activation still uses
whatever ammunition is currently available. Ignored inputs, partial magazines,
empty reserves, and interrupted reloads cannot be detected.

## Weapon selection and preparation

The default selection is PRIMARY, but both weapon magazines initially have the
conservative state `UNKNOWN`. Physical number-row `1` selects PRIMARY and `2`
selects SECONDARY. These are not numeric-keypad keys. Selection keys always
pass through, injected keys are ignored, and auto-repeat is processed once per
physical down/up cycle.

The internally selected weapon is PRIMARY at startup. A selection for the
already selected mode is an internal no-op in every phase, including idle,
preparing, firing, and stopping. It cannot disable, reload, change magazine
state, replace a generation, or emit audio. The physical number key still
reaches the game.

With the default configuration, selection of the *other* weapon does the
following without starting fire: cancel and clean up existing work, select the
requested weapon, mark it
`UNKNOWN`, wait `weapons.switch_settle_ms`, generate the configured `R` press,
wait the complete weapon-specific reload time, then mark it `FULL` and arm it
only if every operation and foreground check succeeded.

Selection preparation is background work. An accepted MB1-down always wins: it
cancels and invalidates unfinished switch-settle/reload preparation without
joining or waiting for that worker, then schedules firing immediately. If
preparation completed and was reconciled first, the magazine may be `FULL`;
otherwise firing begins with `UNKNOWN` ammunition state.

The preparation lifecycle is explicit:

```text
IDLE_UNKNOWN -> PREPARING -> IDLE_FULL_ARMED
IDLE_UNKNOWN -> PREPARING -> PREPARATION_FAILED -> IDLE_UNKNOWN
```

Only the current generation may publish `FULL`; late results from canceled or
replaced workers are ignored. A preparation failure is reported in the console
and cannot leave the controller stuck in `PREPARING`.

`weapons.switch_settle_ms = 500` is an initial tunable value, not a proven
game-specific constant. Increase it if reload occurs before the selected weapon
is ready. Selection never starts firing automatically.

## Immediate MB1 down-edge toggle

An unmodified physical MB1 pair beginning while Helldivers is freshly confirmed
foreground is suppressed. Its accepted physical down edge toggles the selected
macro immediately. MB1-up is cleanup-only: it clears physical/pair state and
never starts, rejects, toggles, prepares, changes weapons, or emits audio. On
the accepted down edge the controller sets `enabled`, queues ON, invalidates
and nonblockingly cancels any background preparation, publishes
`RUNNING_PRIMARY` or `RUNNING_SECONDARY`, and activates generated firing in the
same controller reconciliation. It does not wait for MB1-up, switch settle,
reload input, reload completion, or preparation-thread exit.

The configured policy is explicit:

```toml
[behavior]
start_policy = "immediate"
```

Only `immediate` is supported. Deterministic fake-clock tests schedule the first
generated MB1-down at 0 ms; the application-controlled target is at most 50 ms
from accepted physical MB1-down. Windows scheduling and the game are outside
that measurement.

If the weapon is `FULL`, firing starts without an unnecessary reload. A later
accepted MB1-down disables and starts cleanup. Its matching up remains paired
but causes no controller transition. A 30 ms start debounce follows a stop.

Every genuine physical MB1-down latches exactly one decision:

- `SUPPRESS_TOGGLE`
- `PASS_THROUGH`
- `DEFERRED_BYPASS`

The matching up always uses the latched decision, even if Ctrl, foreground, or
cleanup state changes. Outside Helldivers, or when foreground state is stale or
uncertain, a new MB1 pair passes normally. Generated and other injected events
pass through and cannot control the macro.

## Ctrl+MB1 normal and deferred behavior

The keyboard hook records physical Left/Right Ctrl synchronously before it
queues the higher-level cancellation event. If Ctrl is held, generated MB1 is
not owned, and cleanup is complete, the complete physical MB1 pair passes
through normally and never toggles the macro.

If a rapid Ctrl+MB1 begins while generated MB1 is still down or cancellation
cleanup is pending, the complete physical pair is suppressed and deferred.
Work is performed outside the hook thread in this strict order:

```text
generated MB1-up
bypass MB1-down
bypass MB1-up
```

If physical MB1 remains held after cleanup, tagged bypass MB1-down stays owned
until physical release. If the physical click already ended, a tagged click is
replayed for at least `controls.deferred_bypass_click_ms` (20 ms by default).
Focus loss or input failure discards forwarding and releases any owned input.
Holding Ctrl and waiting for OFF before clicking remains the simplest manual
procedure, but the deferred path protects rapid clicks.

Ctrl disables an enabled macro before canceling work, invalidates the worker
generation, and cannot enter preparation or restore
`enabled`. Ctrl-up and stale worker completion cannot restart it; a later,
distinct unmodified MB1-down is required. Ctrl alone is state-neutral while
the macro is disabled. Ctrl+MB1 always marks the selected magazine `UNKNOWN`.

Physical MB2 is the game's toggle-aim input. Every down/up passes through
unchanged and is never suppressed or replayed. A foreground physical MB2-down
updates a conservative assumed state from `AIM_OFF` to `AIM_ON`, or from
`AIM_ON` to `AIM_OFF`; MB2-up is edge cleanup only. This tracking never enables,
disables, starts, stops, reloads, or emits macro audio. Tagged/generated and
other injected MB2 events are ignored by the hook.

Physical Left and Right Shift are the game's toggle-sprint inputs and always
pass through unchanged. Only the first non-injected down edge while held is
actionable. If the macro is actively firing, Shift disables it, queues exactly
one OFF notification, releases generated MB1, prevents later shots, and begins
one reload-only sequence after the firing worker has retired. Reload is allowed
while sprinting. A valid reload may publish `FULL`, but the macro remains
disabled and MB1 must be pressed again to resume firing.

If Shift arrives after the active macro has already entered its reload phase,
the existing `R` press/reload wait is allowed to finish without cancellation or
a duplicate reload. The worker stops before another firing cycle. Shift while
disabled and idle or during selection preparation is pass-through-only; it does
not change generation, start work, cancel preparation, or emit audio. Shift-up
only clears the physical hook latch and never changes controller state.

The live controller starts with assumed `AIM_OFF`. With the default
`controls.shift_cancels_aim_natively = false`, the first Shift-down sends one
owned/tagged MB2 down/up pair only when the assumed state is `AIM_ON`. It first
moves to `AIM_OFF_PENDING`, then publishes `AIM_OFF` only after successful
delivery. `AIM_OFF` and `UNKNOWN` never generate MB2, so Shift cannot blindly
start aiming. Repeats and Shift-up do not send additional clicks. A physical
MB2 edge or foreground loss invalidates pending output; failure or obsolete
completion leaves the state `UNKNOWN` and is never retried blindly.

If Helldivers itself cancels toggle aim when Shift begins sprinting, set:

```toml
[controls]
shift_cancels_aim_natively = true
```

In that mode Shift emits no generated MB2 and records the assumed state as
`AIM_OFF`. If Shift ever causes aiming to turn on, enable this option.

For narrow Ctrl troubleshooting, set
`diagnostics.ctrl_bypass_logging = true`. It logs only Ctrl cleanup state, MB1
pair decisions, and deferred-forwarding stages.

For controller troubleshooting, set `diagnostics.state_tracing = true`. Each
record has a monotonic sequence number, normalized event, exact event source,
previous/result state values, selected weapon, conservative magazine state,
generation, foreground certainty, elapsed milliseconds, and a transition-local
reason. Old cancellation reasons are never carried into later transitions. A
rejected start uses the same structured record with the `START_REJECTED:`
prefix:

```text
START_REJECTED: seq=<n> elapsed_ms=<ms> event=START_REJECTED source=<source> previous=[...] result=[...] generation=<n> reason=<reason>
```

If firing does not begin, first run `--simulate-session`. If that passes, enable
state tracing for a deliberate live diagnostic and look for `START_REJECTED` or
`Preparation failed for ...`; these distinguish controller rejection,
foreground uncertainty, and ordinary `SendInput` failure. Both diagnostic
options are disabled by default and never log unrelated user input.
`SAME_MODE_SELECTION_IGNORED` is the only optional trace record for an
otherwise inert same-mode selection.

Conditional aim tracing is limited to `AIM_PHYSICAL_ON`, `AIM_PHYSICAL_OFF`,
`AIM_OFF_REQUESTED`, `AIM_OFF_SENT`, `AIM_OFF_SKIPPED`, and `AIM_OFF_FAILED`.

Reload diagnostics use transition-local worker phases `FINAL_SHOT_DOWN`,
`FINAL_SHOT_UP`, `RELOAD_KEY_DOWN`, `RELOAD_KEY_UP`,
`RELOAD_WAIT_STARTED`, `RELOAD_COMPLETED`, and `RELOAD_FAILED`. Each trace has
the selected weapon, generation, worker source/phase, enabled state, elapsed
milliseconds, and reason. For both weapon profiles, `FINAL_SHOT_UP` and
`RELOAD_KEY_DOWN` must carry the same elapsed timestamp.

Confirmed foreground loss is one transaction: disable, invalidate the
generation, cancel work, release owned input, retain the
selected weapon, and mark affected ammunition `UNKNOWN`. Foreground regain
alone never reloads, fires, plays audio, or replays clicks. A physical MB1-up
must establish neutral input before a new toggle is accepted, and a selection
key held across regain cannot create a fresh edge. The matching up of a pair
suppressed before focus loss remains suppressed, preventing half-click leakage.

## Conservative magazine state

The application cannot observe the actual ammunition count. `FULL` means only
that the application successfully issued `R`, remained foreground, and waited
through the complete configured reload interval without cancellation, weapon
switch, bypass input, focus loss, or input failure. `UNKNOWN` means the state
cannot be proved from the application's own completed actions. Primary and
secondary are tracked separately.

A shot immediately marks its weapon `UNKNOWN`. A verified normal-cycle reload
marks it `FULL`; the next shot marks it `UNKNOWN` again. Cancellation before a
verified reload, interrupted preparation, focus loss during firing/reloading,
input failure, shutdown during a cycle, selection, or manual Ctrl+MB1 leaves it
`UNKNOWN`.

Immediate activation deliberately prioritizes latency over a guaranteed-full
magazine. The first cycle uses whatever ammunition is currently available; it
may dry-fire or fire fewer configured shots when the magazine is empty or
partial. PRIMARY's 46-round tactical state cannot be inferred from a completed
reload: if activation started at 45 or below, firing may empty the weapon and
the reload may return only 45. Establish 46 manually before enabling the
synchronized cycle. If a background preparation already issued `R`, firing is
still scheduled and Helldivers decides whether the shot interrupts or is
delayed by its reload/weapon-switch animation. Immediate scheduling does not
mean guaranteed ammunition or guaranteed immediate in-game response.

The game may ignore or interrupt a reload. `FULL` cannot guarantee ammunition
when reserve ammunition is empty. Reloading a partial magazine may discard
remaining ammunition depending on game mechanics. If absolute certainty is
required, verify ammunition in-game. There is no attempt to inspect game memory
or bypass anti-cheat restrictions.

## Exact firing cycles

PRIMARY repeats exactly 45 discrete clicks at the configured 85 ms period.
Shots 1 through 44 are MB1 down 35 ms, up, then wait 50 ms. Shot 45 is MB1 down
at 3740 ms and up at 3775 ms; scan-code `R` goes down immediately at the same
3775 ms timestamp under one short output-serialization boundary. There is no
post-shot wait, controller round trip, worker handoff, or sleeping lock between
the final MB1-up and R-down. `R` goes up at 3800 ms, the 2000 ms reload wait
then runs, and valid completion occurs at 5800 ms. ON is not replayed between
cycles.

SECONDARY repeats exactly 13 shots at the configured 120 ms period. Shots 1
through 12 are MB1 down 35 ms, up, then wait 85 ms. Shot 13 is down at 1440 ms
and up at 1475 ms; `R` goes down immediately at the same 1475 ms timestamp
under the same short output boundary. `R` goes up at 1500 ms, the 2000 ms reload
wait follows, and the complete cycle is 3500 ms. No timing configuration value
was reduced to obtain the zero-gap reload.

These dry-cycle durations start immediately after activation; they do not wait
for selection preparation. Background selection may still add switch-settle
and reload work if it finishes before activation. Every macro/preparation wait
checks cancellation and fresh foreground ownership at 5 ms intervals.

The shared output lock is held only around short consecutive output groups and
cleanup, never during switch-settle, press-duration, or reload waits. Immediate start
signals preparation cancellation without acquiring that lock or joining the
thread. A retired preparation may release only its owned `R`; it cannot release
the current macro's MB1. State and generated-input ownership are published
before the firing worker is activated.

## Audio

- ON: 1000 Hz for 100 ms, queued once on an accepted immediate enable edge.
- OFF: 500 Hz for 150 ms, exactly once when an enabled macro is disabled. Shift
  queues OFF immediately before the allowed sprint reload; other stop paths
  retain their deterministic cleanup ordering.

Preparation, selection, startup, and idle cancellation are silent. Audio uses a
dedicated FIFO thread and cannot delay hook callbacks, input release, or timing.
`--test-audio` prints `Playing ON signal...`, then `Playing OFF signal...`, and
finally `Audio test complete.` It drains both accepted tones before returning;
background playback failures are returned to the command instead of being
silently swallowed. Automated tests use fake audio and never call `winsound`.

## Commands

```powershell
python main.py --check-config
python main.py --identify-foreground --delay 5
python main.py --dry-run-primary-cycle
python main.py --dry-run-secondary-cycle
python main.py --simulate-session
python main.py --test-audio
python main.py --live
```

Only `--live` installs hooks, suppresses paired physical MB1, or generates
input. `--test-audio` plays only the configured tones. Dry runs, simulation,
and foreground identification do not install hooks, send input, suppress input,
access the game, wait in real time, or play sound. Simulation prints scenarios
A through AC and ends with `DETERMINISTIC CONTROL SIMULATION: PASS` only after
the existing controller regressions plus immediate UNKNOWN start, switch-settle
preemption, active-reload preemption, immediate stop, and first-cycle reload
synchronization all pass. Scenario V verifies zero-gap SECONDARY reload;
scenarios W through AA preserve PRIMARY, sprint-reload, and physical toggle-aim
coverage; scenarios AB and AC verify conditional aim-off delivery, stale
invalidation, foreground cleanup, and native Shift mode.

The complete non-live validation set is:

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

## Platform limitations

Logitech G815 G-keys are excluded because they are vendor-specific rather than
standard number-row events. No Logitech G HUB, Lua, AutoHotkey, driver, helper
executable, or third-party Python package is used.

Windows can remove a low-level hook if a callback stalls; callbacks here only
update small physical state, latch a decision, and enqueue work. Hook/message
loop failure cancels work. UIPI or the game may reject `SendInput`. If ordinary
input is rejected, stop using live mode; no bypass is implemented. Python can
schedule promptly but cannot force Helldivers to accept or immediately act on
input during a weapon-switch/reload animation.

The application cannot observe game animation state or whether the game accepts
an ordinary generated `R`. If diagnostics prove final MB1-up and R-down share a
timestamp but the game still displays inactivity, that remaining gap is
game-side animation/input acceptance rather than an application sleep. The
application does not send blind reload retries.

Aim state is inferred only from foreground physical MB2 edges and successful
owned output. Game-side aim changes, missed events, focus changes, UI actions,
or rejected input can desynchronize that assumption. Foreground loss and
ambiguous pending-output races therefore set it to `UNKNOWN`; Shift never sends
a blind MB2 toggle from `UNKNOWN`.
