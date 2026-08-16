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
6. Review both dry runs before deliberately choosing `--live`.

Running `python main.py` without a mode prints help and does nothing.

## Weapon selection and preparation

The default selection is PRIMARY, but both weapon magazines initially have the
conservative state `UNKNOWN`. Physical number-row `1` selects PRIMARY and `2`
selects SECONDARY. These are not numeric-keypad keys. Selection keys always
pass through, injected keys are ignored, and auto-repeat is processed once per
physical down/up cycle.

With the default configuration, selection does the following without starting
fire: cancel and clean up existing work, select the requested weapon, mark it
`UNKNOWN`, wait `weapons.switch_settle_ms`, generate the configured `R` press,
wait the complete weapon-specific reload time, then mark it `FULL` and arm it
only if every operation and foreground check succeeded.

`weapons.switch_settle_ms = 500` is an initial tunable value, not a proven
game-specific constant. Increase it if reload occurs before the selected weapon
is ready. Selection never starts firing automatically.

## MB1 toggle and reload-before-start

An unmodified physical MB1 pair beginning while Helldivers is freshly confirmed
foreground is suppressed. The first complete click requests the selected macro
only after physical MB1-up. If the selected weapon is `UNKNOWN`, reload
preparation runs first and ON is not played. During preparation, at most one
start request is queued; successful preparation starts firing and plays ON.
Focus loss or cancellation clears that pending request.

If the weapon is `FULL`, firing starts without an unnecessary reload. A later
complete MB1 click stops firing, with cancellation beginning on its down event.
A 30 ms start debounce follows a stop.

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

Ctrl+MB1 always marks the selected magazine `UNKNOWN`. Either Shift key and
physical right click also cancel active work while passing through normally.

For narrow troubleshooting, set
`diagnostics.ctrl_bypass_logging = true`. It logs only Ctrl cleanup state, MB1
pair decisions, and deferred-forwarding stages. It is disabled by default and
does not log unrelated keyboard or mouse input.

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

The game may ignore or interrupt a reload. `FULL` cannot guarantee ammunition
when reserve ammunition is empty. Reloading a partial magazine may discard
remaining ammunition depending on game mechanics. If absolute certainty is
required, verify ammunition in-game. There is no attempt to inspect game memory
or bypass anti-cheat restrictions.

## Exact firing cycles

PRIMARY repeats three charged shots: MB1 down 900 ms, up, wait 20 ms; MB1 down
900 ms, up, wait 20 ms; MB1 down 900 ms, up, wait 300 ms; scan-code `R` down
25 ms, up, then wait 2600 ms. The firing cycle is 5665 ms.

SECONDARY repeats exactly 13 shots. Each is MB1 down 35 ms, up, then wait
145 ms, including after shot 13. Each period is 180 ms. It then generates
scan-code `R` down 25 ms, up, waits 2000 ms, and repeats. The firing cycle is
4365 ms.

These dry-cycle durations describe firing after preparation. Selection also
adds the configured switch-settle time and a full reload sequence. Every wait
checks cancellation and fresh foreground ownership at 5 ms intervals.

## Audio

- ON: 1000 Hz for 100 ms, only when actual macro firing begins.
- OFF: 500 Hz for 150 ms, exactly once when a running macro stops after cleanup.

Preparation, selection, startup, and idle cancellation are silent. Audio uses a
dedicated FIFO thread and cannot delay hook callbacks, input release, or timing.

## Commands

```powershell
python main.py --check-config
python main.py --identify-foreground --delay 5
python main.py --dry-run-primary-cycle
python main.py --dry-run-secondary-cycle
python main.py --test-audio
python main.py --live
```

Only `--live` installs hooks, suppresses paired physical MB1, or generates
input. `--test-audio` plays only the configured tones. Dry runs and foreground
identification do not install hooks, send input, suppress input, or play sound.

## Platform limitations

Logitech G815 G-keys are excluded because they are vendor-specific rather than
standard number-row events. No Logitech G HUB, Lua, AutoHotkey, driver, helper
executable, or third-party Python package is used.

Windows can remove a low-level hook if a callback stalls; callbacks here only
update small physical state, latch a decision, and enqueue work. Hook/message
loop failure cancels work. UIPI or the game may reject `SendInput`. If ordinary
input is rejected, stop using live mode; no bypass is implemented.
