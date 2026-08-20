# Helldivers primary/secondary macro (Windows 11 Pro)

This project is a visible, foreground-restricted macro using Python 3.11+
standard-library modules and ordinary Windows APIs through `ctypes`. It does
not use administrator privileges, game memory, screen recognition, DLL
injection, network inspection, hardware emulation, concealed automation, or an
anti-cheat bypass. Verify the game's current rules before use.

Ordinary `SendInput` can fail at the Windows API boundary or be accepted into
the Windows input stream but ignored by the game. An API failure stops work and
releases owned inputs; this project does not provide a UIPI, integrity-level,
anti-cheat, or elevated-mode bypass.

## Setup and safe inspection

1. Install 64-bit Python 3.11 or newer on Windows 11 Pro.
2. Open PowerShell in this directory. No packages need installation.
3. Set Helldivers' Aim behavior to **Hold**. Toggle aim is not supported.
4. Review `config.toml`. The default target is `helldivers2.exe`, and
   `controls.aim_mode` must be `"hold"`.
5. Run `python main.py --check-config`.
6. Start the game yourself, make it foreground, and run
   `python main.py --identify-foreground --delay 5` to inspect the owning
   process without hooks, input, suppression, or sound.
7. Run `python main.py --simulate-session`. This exercises deterministic
   scenarios A through BA with fake hooks, input, foreground, time, workers,
   and audio only.
8. Review the weapon and stratagem dry runs before deliberately choosing `--live`.

Running `python main.py` without a mode prints help and does nothing.

## Stratagem macros (F23/F24)

The two foreground-only standard Windows trigger keys are:

- F23: four-target stratagem macro
- F24: Resupply followed by Reinforce

Required Helldivers controls are Left Ctrl for opening/holding the stratagem
menu, arrow keys for directions, and MB1 for throw/activate. PRIMARY weapon
mode remains Automatic. Do not bind Fire to P for this setup.

The exact F23 entries are `DOWN UP RIGHT RIGHT UP`, `DOWN UP RIGHT LEFT`,
`DOWN UP RIGHT UP LEFT UP`, and `DOWN UP RIGHT RIGHT LEFT`. F24 enters Resupply
as `DOWN DOWN UP RIGHT`, then Reinforce as `UP DOWN RIGHT LEFT UP`. Each entry
holds generated Left Ctrl, uses tagged extended scan-code arrows, releases
Ctrl, sends one separately owned tagged MB1 click, and retains the final 800 ms
delay. Safe fake durations are 4,160 ms for F23 and 2,040 ms for F24.

G1/G2 may be remapped externally to the ordinary F23/F24 keys. Python listens
only for those standard Windows key events and has no runtime dependency on G
Hub or Lua. Triggers work only while `helldivers2.exe` is the certain foreground
target and weapon firing is disabled with no reload, preparation, bypass, or
Shift transaction active. Busy presses are consumed, rejected, and
never queued for later.

One physical trigger pair is latched and suppressed; auto-repeat does not
retrigger, and a release plus new press is required. During a sequence MB1 and
weapon-selection controller actions are blocked. RMB, either Shift key,
foreground loss/uncertainty, shutdown, Ctrl+C, hook/backend failure, or an
input failure cancels the sequence and releases only that worker's token-owned
Ctrl, arrow, and action MB1. Shift still replays the existing sprint action. A
physical RMB-down can cancel an active stratagem, but that busy press is not
delayed into weapon activation and must be released before a fresh foreground
RMB-down can arm firing. Stratagems never generate R, P, or weapon ON/OFF audio.

```toml
[stratagems]
enabled = true
four_target_trigger = "f23"
support_trigger = "f24"
key_press_ms = 20
key_gap_ms = 20
ctrl_settle_ms = 20
action_press_ms = 20
action_delay_ms = 800
```

## Primary automatic-hold profile and calibration

Configure the AR-2 to **Automatic** in Helldivers and keep Fire bound to MB1.
Physical MB1 remains the macro ON/OFF toggle. Once an RMB-held activation is
accepted, the generated ownership-tagged MB1 is held continuously; Helldivers,
not Python, controls the weapon's automatic cadence. No additional P binding is
required.

```toml
[output]
fire_device = "mouse"
```

`fire_device` accepts exactly `"keyboard"` or `"mouse"`. Mouse mode needs no
scan code. The tested keyboard scan-code backend remains available as an
inactive optional mode, but it is not part of normal setup.

The retained live-tested AR-2 reference in `profiles/ar2-coyote.toml` is:

```toml
[primary]
fire_mode = "automatic_hold"
automatic_hold_ms = 4450
post_fire_reload_delay_ms = 0
reload_press_ms = 25
reload_wait_ms = 2000
```

At 600 RPM, shots are nominally 100 ms apart: the 45th is expected near 4,400
ms and the 46th near 4,500 ms. The initial 4,450 ms hold aims to release after
45 rounds but before the chambered 46th. Calibrate only
`primary.automatic_hold_ms`; the application cannot observe muzzle events or
ammunition, so this remains a manual live measurement.

The tactical-reload strategy assumes a maximum loaded state of 46: start at 46,
allow automatic fire to consume 45, leave one chambered, reload, and return to
46. Python cannot count accepted automatic shots.

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

Selection preparation is background work. An RMB-held, accepted MB1-down always wins: it
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

## Hold-to-aim and immediate MB1 down-edge toggle

Set Helldivers Aim behavior to **Hold**. Hold physical RMB to aim, then press
physical MB1 once to start the selected macro. Firing authority is based only
on a current, valid physical foreground RMB hold; the application does not
infer or alternate a game-side toggle state.

The controller starts `RMB_RELEASED`. A fresh untagged, non-injected physical
RMB-down observed while Helldivers is `ACTIVE_CERTAIN` establishes
`RMB_HELD_VALID`; repeats do nothing. Physical RMB-up returns to
`RMB_RELEASED`. Tagged/generated RMB, foreign injected RMB, and background or
uncertain RMB cannot arm firing.

Normal use is: hold RMB, press physical MB1 once, and keep RMB held. Release
RMB, press either Shift, press MB1 again, or lose foreground to stop safely.
After a hit or stagger removes aim, release and press RMB again if Helldivers
does not resume hold aim automatically, then press MB1 to restart if desired.
Hold mode removes toggle inversion, but the application still cannot prove the
crosshair or the game's actual aim state; its permission represents only
current valid physical RMB hold intent.

An unmodified physical MB1 pair beginning while Helldivers is freshly confirmed
foreground is suppressed. Its down edge can enable the selected macro only
while RMB is validly held. Otherwise it is rejected with `RMB_HOLD_REQUIRED`:
no worker, generated fire, reload, ammunition transition, or ON audio is
created. MB1-up remains cleanup-only. On an accepted down edge the controller
sets `enabled`, queues ON, invalidates and nonblockingly cancels background
preparation, publishes `RUNNING_PRIMARY` or `RUNNING_SECONDARY`, and activates
generated firing immediately.

The configured policy is explicit:

```toml
[behavior]
start_policy = "immediate"
```

Only `immediate` is supported. Deterministic fake-clock tests schedule the first
generated MB1-down at 0 ms after an RMB-held accepted edge; the
application-controlled target is at most 50 ms from accepted physical MB1-down.
Windows scheduling and the game are outside that measurement.

If the weapon is `FULL`, firing starts without an unnecessary reload. A later
accepted MB1-down disables and starts cleanup. Its matching up remains paired
but causes no controller transition. A 30 ms start debounce follows a stop.

Every genuine physical MB1-down latches exactly one decision:

- `SUPPRESS_TOGGLE`
- `SUPPRESS_STRATAGEM_BUSY`
- `PASS_THROUGH`
- `DEFERRED_BYPASS`

The matching up always uses the latched decision, even if Ctrl, foreground, or
cleanup state changes. Outside Helldivers, or when foreground state is stale or
uncertain, a new MB1 pair passes normally. Generated and other injected events
pass through and cannot control the macro.

## Ctrl+MB1 normal and deferred behavior

The keyboard hook records physical Left/Right Ctrl synchronously before it
queues the higher-level cancellation event. If Ctrl is held, generated fire
input is not owned, and cleanup is complete, the complete physical MB1 pair passes
through normally and never toggles the macro.

Ctrl+MB1 is the explicit normal-click bypass for menus and manual interaction.
The RMB-hold-required firing gate applies only to unmodified MB1 macro activation;
the bypass never enables a worker or plays ON/OFF audio.

If a rapid Ctrl+MB1 begins while generated fire input is still down or cancellation
cleanup is pending, the complete physical pair is suppressed and deferred.
Work is performed outside the hook thread in this strict order:

```text
generated automatic MB1-up
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

Physical RMB passes through unchanged as the game's hold-to-aim input. RMB-down
alone never starts or restarts firing; a new physical MB1 activation is always
required. RMB-up during PRIMARY automatic hold or any SECONDARY firing phase
removes authority immediately. The generated cleanup order is:

```text
MACRO_DISABLED
owned generated MB1-up, if fire was held
FIRING_STOPPED / OFF once
```

RMB-up never generates MB2 or `R`. If reload already legitimately began, the
existing reload may finish under the existing policy without a duplicate `R`.
Pressing RMB again only establishes hold authority; physical MB1 is still
required to restart.

Physical Left and Right Shift are the game's toggle-sprint inputs. While the
target is confirmed foreground, the hook pair-latches and suppresses one
physical Shift down/up pair, including repeat downs, and queues one deferred
transaction carrying the same Left/Right scan code. Outside Helldivers the pair
passes through unchanged and creates no controller event. Tagged/generated
Shift events are ignored by the hook and cannot recurse.

If Shift occurs while physical RMB is validly held, the deferred transaction is
ordered as follows:

```text
disable active firing and release owned generated MB1, if needed
one ownership-tokened, tagged MB2-up to neutralize the held aim
owned replay of the same physical Shift scan-code down/up
```

The hold release therefore completes before Helldivers receives the sprint toggle. If
active firing was stopped, OFF is queued exactly once and the selected magazine
is marked conservatively. Shift never presses `R` and never creates a
reload-only worker. While disabled and idle it changes no macro state,
ammunition state, generation, or audio. During weapon-selection preparation it
does not cancel the existing preparation or create a second reload.

If Shift arrives after a normal macro cycle has already entered its reload
phase, that existing `R` press and reload wait may finish and publish its valid
result. Shift does not cancel it merely because sprinting permits reload, and
does not press `R` again. The macro remains disabled afterward; MB1 must be
pressed again to resume firing.

Shift changes `RMB_HELD_VALID` to `RMB_HELD_REARM_REQUIRED`. While the original
physical RMB pair remains down, repeats and later Shift presses cannot rearm it
or emit duplicate hold-release output. The user must physically release RMB,
then press it again in confirmed foreground. No generated RMB-down is ever
fabricated.

Sprint remains a persistent game-side toggle. A second deliberate physical
Shift creates exactly one second replay pair so the game can toggle sprint OFF.
The only supported aim configuration is:

```toml
[controls]
aim_mode = "hold"
```

Unsupported values are rejected before hooks start. Toggle aim is not
supported because hit, stagger, collision, animation, and other game-side
cancellations cannot be observed reliably.

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

Deferred sprint tracing is limited to low-volume transitions including
`SHIFT_DEFERRED`, `SHIFT_TRANSACTION_STARTED`,
`RMB_HOLD_RELEASE_REQUESTED`, `RMB_HOLD_RELEASED_FOR_SHIFT`,
`SHIFT_REPLAY_DOWN`, `SHIFT_REPLAY_UP`, `SHIFT_TRANSACTION_COMPLETED`, and
`SHIFT_TRANSACTION_FAILED`. Hold gating uses `RMB_HOLD_ESTABLISHED`,
`RMB_HOLD_RELEASED`, and `RMB_HOLD_REQUIRED_REJECTED`.

Reload diagnostics use transition-local worker phases `FINAL_SHOT_DOWN`,
`FINAL_SHOT_UP`, `RELOAD_KEY_DOWN`, `RELOAD_KEY_UP`,
`RELOAD_WAIT_STARTED`, `RELOAD_COMPLETED`, and `RELOAD_FAILED`. Each trace has
the selected weapon, generation, worker source/phase, enabled state, elapsed
milliseconds, and reason. For both weapon profiles, `FINAL_SHOT_UP` and
`RELOAD_KEY_DOWN` must carry the same elapsed timestamp.

Initial non-target or uncertain observations establish a startup baseline and
leave RMB authority released. The controller records when
Helldivers is first observed `ACTIVE_CERTAIN`; only a later inactive/uncertain
transition is a genuine foreground loss. Confirmed genuine foreground loss is
one transaction: disable, invalidate the
generation, cancel work, release owned input, invalidate any held RMB authority,
retain the
selected weapon, and mark affected ammunition `UNKNOWN`. Foreground regain
alone never reloads, fires, plays audio, or replays clicks. A physical MB1-up
must establish neutral MB1 input before a new MB1 toggle is accepted, and a selection
key held across regain cannot create a fresh edge. The matching up of a pair
suppressed before focus loss remains suppressed, preventing half-click leakage.

If foreground is lost while RMB is physically held, regaining foreground does
not rearm it. The user must release RMB, press RMB again in confirmed
foreground, then press MB1. A stale held RMB is never promoted merely because
the game regains focus.

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

PRIMARY emits one tagged MB1-down at 0 ms, holds it for 4,450 ms, then emits
MB1-up and `R`-down consecutively under one short output boundary. `R` goes up
at 4,475 ms, the 2,000 ms reload wait follows, and the cycle completes at
6,475 ms. ON is not replayed between cycles. A manual stop during the hold
releases MB1 promptly and emits no `R`.

SECONDARY remains tap mode and repeats exactly 13 tagged MB1 presses at the
configured 120 ms period. Shots 1 through 12 are MB1 down 35 ms, up, then wait
85 ms. Shot 13 is down at 1,440 ms and up at 1,475 ms; `R` goes down
immediately at the same 1,475 ms timestamp
under the same short output boundary. `R` goes up at 1500 ms, the 2000 ms reload
wait follows, and the complete cycle is 3500 ms. No timing configuration value
was reduced to obtain the zero-gap reload.

These dry-cycle durations start immediately after activation; they do not wait
for selection preparation. Background selection may still add switch-settle
and reload work if it finishes before activation. Every macro/preparation wait
checks cancellation and fresh foreground ownership at 5 ms intervals.

The shared output lock is held only around short consecutive output groups and
cleanup, never during switch-settle, click/key press-duration, or reload waits. Immediate start
signals preparation cancellation without acquiring that lock or joining the
thread. A retired preparation may release only its owned `R`; it cannot release
the current macro's MB1. State and generated-input ownership are published
before the firing worker is activated.

Live mode requests a 1 ms Windows multimedia timer resolution for its complete
lifecycle. The relative firing sequence and 5 ms interruptible cancellation
polls are unchanged; the resolution request allows those short waits to use a
finer Windows timer quantum instead of repeatedly inheriting the default. The request is
reference-counted and matched by `timeEndPeriod` after normal shutdown,
Ctrl+C, startup/hook failure, or an exception. If acquisition fails, live mode
prints one warning and continues with the documented default-resolution
fallback. Configuration checks, dry runs, simulation, and tests never activate
the live timer lease. No absolute shot deadlines, rebasing, catch-up output,
busy spinning, or priority changes are used.

## Opt-in cadence diagnostics

Dry-run counts cannot prove that Windows delivered each owned event through the
hook or that Helldivers accepted it. For one deliberate manual live test, use:

```powershell
python main.py --live --cadence-diagnostics
```

The flag is valid only with `--live` and is disabled by default. It arms on the
first generated PRIMARY MB1-down, captures one MB1 hold pair and the immediately
following `R` pair, then freezes.
Later cycles are counted as ignored diagnostic events but continue normally.
If that first phase is canceled, the capture freezes incomplete instead of
merging a later activation into it. Physical input, generated aim/sprint input,
and unrelated keyboard input are never recorded. During the bounded capture,
only configured MB1/R injected metadata is retained. Cursor coordinates and physical
mouse input are never retained.
Both the SendInput and hook structures use one canonical unsigned
pointer-width `ULONG_PTR`; the hook dereference and comparison preserve and
normalize all pointer-width bits. The canonical marker is the live-observed-safe
32-bit value `0x43524f31`, stored in pointer-width fields for every generated
device.
Recording performs no console/file output, wait, backend call, or output-lock
acquisition from the hook. After Ctrl+C and safe shutdown, one summary reports:

- `captured_primary_cycles`, `capture_complete`, and
  `extra_events_ignored_after_capture`;
- intended MB1/R counts for the active automatic mouse-hold cycle;
- every bounded backend call's requested/accepted count, before/after time,
  call duration, failures, and recent last-error values;
- the backend-dispatch MB1 hold duration independent of hook observation;
- owned hook-observed, passed, suppressed, and controller-routed counts;
- injected keyboard and mouse callback marker match/mismatch counts;
- the backend marker alongside bounded hook-observed `dwExtraInfo` values;
- cleanup releases, final fire-up/R-down times and their gap;
- per-device/action pending expected and unmatched observed counts, plus
  bounded injected-mouse and anomaly records.

For a useful capture, manually establish 46 rounds, set the AR-2 to Automatic,
hold RMB, and start one complete PRIMARY cycle. The recorder freezes
after its `R` pair, so Ctrl+C can be pressed later without contaminating the
capture with subsequent cycles. Preserve the full `CADENCE DIAGNOSTICS SUMMARY` and report it
along with the observed ammunition immediately before reload. An accepted
`SendInput` count proves Windows accepted the event array, not that the game
consumed the shot; matching hook counts and timing isolate that remaining
game-side boundary.

## Audio

- ON: 1000 Hz for 100 ms, queued once on an RMB-held accepted immediate enable edge.
- OFF: 500 Hz for 150 ms, exactly once when an enabled macro is disabled. Shift
  or physical RMB-up queues OFF once when it stops active firing; idle input
  and hold-required rejection are silent.

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
python main.py --dry-run-stratagem-four
python main.py --dry-run-stratagem-support
python main.py --simulate-session
python main.py --test-audio
python main.py --live
python main.py --live --cadence-diagnostics
```

Only `--live` installs hooks, suppresses paired physical MB1/foreground Shift,
or generates input. Physical RMB continues to pass through as hold-to-aim.
`--test-audio` plays only the configured
tones. Dry runs, simulation,
and foreground identification do not install hooks, send input, suppress input,
access the game, wait in real time, or play sound. Simulation prints scenarios
A through BA and ends with `DETERMINISTIC CONTROL SIMULATION: PASS` only after
the existing controller regressions plus RMB-held immediate UNKNOWN-ammunition start, switch-settle
preemption, active-reload preemption, immediate stop, and first-cycle reload
synchronization all pass. Scenario V verifies zero-gap SECONDARY reload;
scenarios W through AC preserve timing, reload, and hold-to-aim regressions;
scenarios AD through AJ verify firing/idle hold-release ordering,
persistent-sprint RMB isolation, a second sprint toggle, existing-reload
preservation, zero Shift-created `R`, and foreground cleanup. Scenarios AK
through AS verify both RMB-released rejections, FIFO RMB-hold/start, RMB-up stops
for both weapons, persistent-sprint RMB/Shift paths, foreground cancellation,
and preservation of an already-started reload without duplicate `R`. Scenario
AT reproduces PowerShell-at-launch, first Helldivers acquisition, physical RMB,
and MB1 reaching `MACRO_ENABLED` and `FIRING_STARTED`.
Scenarios AU through AZ verify the exact F23/F24 sequences and durations,
repeat/pair gating, background/uncertain/owned-event filtering, all busy-state
rejections, active input exclusion, RMB/Shift/foreground/shutdown cleanup, and
cancellation throughout every sequence timing phase and arrow position.
Scenario BA models repeated hit/stagger recovery gestures and proves release/
press cycles never invert firing eligibility.

The complete non-live validation set is:

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

## Platform limitations

Logitech G-keys are vendor-specific, so this project does not read them
directly. They may be mapped externally to standard F23/F24 events; the Python
runtime does not use Logitech G HUB, Lua, AutoHotkey, a driver, a helper
executable, or a third-party Python package.

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

The application cannot inspect the crosshair or the game's actual aim or
persistent-sprint state. It tracks only valid physical RMB hold intent. Hold
mode removes the alternating toggle assumption, so a game-side cancellation
cannot invert an inferred on/off state. If Helldivers does not
resume aim while RMB remains held after a hit, stagger, collision, or animation,
release and press RMB again. There is no OCR, screen capture, game-memory read,
polling, blind toggle retry, or automatic guess.
