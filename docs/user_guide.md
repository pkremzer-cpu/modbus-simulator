# User guide

> This guide is a stub. It is fleshed out in roadmap step 7, once the GUI is implemented.
> Screenshot placeholders are marked `[SCREENSHOT: description]` — the user provides the
> final images.

## First launch

[SCREENSHOT: language picker dialog on first start, magyar / English radio]

## Main window

[SCREENSHOT: full main window with all six tabs visible, Server tab selected]

## Server tab

[SCREENSHOT: register table, start/stop button, IP/port fields]

## Client tab

[SCREENSHOT: manual transaction panel on left, polling list on right]

## Traffic log

[SCREENSHOT: live table with RX/TX rows, filter bar on top]

## Trend chart

[SCREENSHOT: multi-line chart of three registers over 60 s window]

## Simulation editor

[SCREENSHOT: per-register generator editor — Constant / Ramp / Sine / Random / Script tabs]

## Exception rules

[SCREENSHOT: rule list with FC / address range / exception code / probability columns]

## Configuration files

The app saves its last session to:

```
~/Library/Application Support/ModbusSimulator/last_session.json
```

Manual save/load is in the *File* menu (⌘S / ⌘O).

## Troubleshooting

- **Port 502 permission denied:** either run the app with `sudo`, or switch to port 5020
  (the default).
- **Firewall prompt on server start:** macOS asks for incoming connection permission.
  Grant it, or add the app under *System Settings → Network → Firewall → Options*.
