# Pulse Secure + FPT-CA token auto-connect

Status: done except autostart (2026-09-28). The tool is deployed to other machines;
this machine is used only for testing (VPN left disconnected, auto-connect off).

## Outcome

A Windows desktop app (`VpnAuto.exe`, window + tray) keeps the Pulse Secure connection up.
- It connects whenever the VPN server is reachable.
- It types the FPT-CA token PIN whenever the "Verify User PIN" dialog appears, including the re-prompt after about 5 h.
- It fills the VPN account and ticks "Save settings".
- Configuration is done in the window or through `vpn_auto.ini`, which lets the same package go to many machines.

## Feasibility (verified live)

| Step | Mechanism | Result |
|---|---|---|
| Read status | text of `JAM_LISTITEM` children; button id 10104 = Connect/Cancel/Disconnect | works, also with the window hidden |
| Click Connect/Cancel | `WM_LBUTTONDOWN/UP` posted to `JAM_BitmapButton` | works |
| Token PIN dialog | `#32770` in Pulse.exe, Edit 1000, Login 2000 | `WM_CHAR` per character is needed; `WM_SETTEXT` leaves Login disabled |
| Account dialog | user 10111, password 10115, Save 10145, Connect 10201 | connected; Save settings persisted the account |

Findings from the live tests:
- Server authentication has two factors: the token certificate (PIN) and the account password.
- The PIN stays cached inside Pulse.exe until that process restarts.
- After Disconnect, the status reads "Disconnecting" for 1-3 min while the button already says "Connect". A click in that window only queues "Connect requested", so the watchdog waits for "Disconnected" before clicking.
- `Pulse.exe -tray` starts without a main window. `-show` creates the window, then the Close button (1021) hides it again without destroying it.

## Acceptance criteria

1. Disconnected -> reconnects without user input - passed live
2. PIN dialog filled at any time - passed live (fresh Pulse process); the 5 h re-prompt was not observed
3. Account filled, "Save settings" ticked - passed live; later connects did not prompt
4. Wrong PIN/password -> auto-fill stops - unit tests only (a lockout must not be provoked)
5. Works with the Pulse window hidden - passed live
6. Deployment through `vpn_auto.ini` - passed (isolated run with fake secrets)
7. Unit tests - 38 passed

## Open

- Start with Windows: the Startup-folder shortcut was blocked by the tool permission policy; left to the user.
- Not tested: a real network loss (cable or Wi-Fi), a locked screen, the natural 5 h PIN re-prompt.
