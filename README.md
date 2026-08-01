# BG3Bridge — BG3SE External Bridge MVP

> **Scope:** Persistent State tracking (`DialogStarted`, `DialogActorJoined`, `EnteredCombat`, `LeftCombat`) → JSON file → Python watcher.
> This is Phase 2 for the Neuro AI co-host. No WebSockets yet, but features rolling event logs and character-name mapping.

---

## Architecture

```
BG3 (running) ──► BG3SE Lua sandbox ──► Ext.IO.SaveFile ──► neuro_state.json
                                                                      │
                                              Python watcher ─────────┘
                                              (polls / inotify)
                                                      │
                                              stdout: parsed JSON + bridge timestamp
```

---

## Prerequisites

| Tool | Where to get it |
|---|---|
| **BG3 Script Extender v32** | https://github.com/Norbyte/bg3se/releases |
| **Python 3.8+** | https://www.python.org |
| `watchfiles` (optional, recommended) | `pip install watchfiles` |

---

## Step 1 — Install BG3 Script Extender

1. Download the **latest release** (v32, `bg3_se_*.zip`) from the [releases page](https://github.com/Norbyte/bg3se/releases).
2. Extract and copy **`DWrite.dll`** into:
   ```
   <Steam>\steamapps\common\Baldurs Gate 3\bin\
   ```
   The default Steam path is usually:
   ```
   C:\Program Files (x86)\Steam\steamapps\common\Baldurs Gate 3\bin\
   ```
3. Create (or edit) **`ScriptExtenderSettings.json`** in the same `bin\` folder:
   ```json
   {
       "CreateConsole": true,
       "LogLevel": "Warning"
   }
   ```
   `CreateConsole: true` opens the SE debug console window when the game starts — **you need this to verify the event fires**.

4. Launch BG3 via Steam once to confirm SE loaded. You should see a separate console window titled **"Baldur's Gate 3 Script Extender"** appear.

---

## Step 2 — Install the BG3Bridge Mod

### Option A — Manual (recommended for development)

Copy the `mod/Mods/BG3Bridge/` folder into your BG3 local mods directory:

```
%LocalAppData%\Larian Studios\Baldur's Gate 3\Mods\BG3Bridge\
```

The full expected structure after copying:

```
%LocalAppData%\Larian Studios\Baldur's Gate 3\Mods\
└── BG3Bridge\
    ├── meta.lsx
    └── ScriptExtender\
        ├── Config.json
        └── Lua\
            ├── BootstrapServer.lua   ← event listener + file writer
            └── BootstrapClient.lua   ← placeholder
```

### Option B — BG3 Mod Manager

Pack the mod folder into a `.pak` using [BG3 Modders Multitool](https://github.com/ShinyHobo/BG3-Modders-Multitool), then load via [BG3 Mod Manager](https://github.com/LaughingLeader/BG3ModManager).

> **For development, Option A (loose files) is much faster — no repacking needed.**

### Enable the Mod in-game

1. Open BG3.
2. Go to **Main Menu → Mods**.
3. Find **BG3Bridge** in the available mods list and enable it.
4. Apply & restart if prompted.

> **Alternatively**, add the mod UUID to your `modsettings.lsx` manually. The UUID is: `a7f3c812-5e2d-4b9a-8c1f-d6e04a2b7f93`

---

## Step 3 — Verify the Output File Path

The Lua mod calls:
```lua
Ext.IO.SaveFile("neuro_state.json", json_str)
```

BG3SE resolves this relative path to:
```
%LocalAppData%\Larian Studios\Baldur's Gate 3\Script Extender\neuro_state.json
```

Typical full path:
```
C:\Users\<YourName>\AppData\Local\Larian Studios\Baldur's Gate 3\Script Extender\neuro_state.json
```

---

## Step 4 — Run the Python Watcher

Open a terminal (PowerShell or CMD) in the project folder:

```powershell
# Recommended: install watchfiles for near-instant detection
pip install watchfiles

# Run the watcher (auto-detects path)
python neuro_watcher.py

# Or with an explicit path:
python neuro_watcher.py --path "C:\Users\<YourName>\AppData\Local\Larian Studios\Baldur's Gate 3\Script Extender\neuro_state.json"

# Force polling mode (fallback):
python neuro_watcher.py --no-watchfiles --interval 0.25
```

Expected output while waiting:
```
────────────────────────────────────────────────────────────────────────
  BG3Bridge Watcher  —  MVP v0.1
────────────────────────────────────────────────────────────────────────
  Watching : C:\Users\...\neuro_state.json
  Started  : 2026-07-31 00:15:00
  Python   : 3.12.0

Trigger a dialogue in BG3 to test. Press Ctrl+C to stop.
```

---

## Step 5 — Trigger the Event In-Game

1. Load (or start) any **single-player campaign save**.
2. Walk up to **any NPC** (a guard, merchant, or story NPC).
3. Click on them to **initiate dialogue**.
4. As soon as the dialogue screen opens:
   - The BG3SE console should print:
     ```
     [BG3Bridge] DialogStarted fired!
     [BG3Bridge]   dialog      = <DialogResourceName>
     [BG3Bridge]   instanceID  = <number>
     [BG3Bridge]   actors      = (none yet — see actors_note)
     [BG3Bridge]   timestamp_ms= <ms>
     [BG3Bridge] Wrote payload to: neuro_state.json
     ```
   - The Python watcher should print:
     ```
     ────────────────────────────────────────────────────────────────────────
       [00:15:42.831]  BG3 EVENT RECEIVED
     ────────────────────────────────────────────────────────────────────────
       event      : DialogStarted
       dialog     : <DialogResourceName>
       instanceID : <number>
       actors     : (none at start — see actors_note)
       game_ms    : 183421 ms (monotonic)
       bridge_ts  : 00:15:42.831
     ```

---

## Known Behaviors & Gotchas

### Actor list dynamically populates
The `dialog_actors` field will typically be empty at `DialogStarted` because actors join the dialogue instance slightly later via `DialogActorJoined`. The bridge successfully captures these additions and updates the persistent state object as they join, translating internal IDs to their English localized names (e.g., "Kagha", "Shadowheart").

### Combat Logs filtered
The engine considers many items (torches, chests) to enter combat. The bridge strictly filters `EnteredCombat` and `LeftCombat` events to only trigger for Character entities (`Osi.IsCharacter`), preventing log spam from inanimate objects.

### SE console is not appearing
Confirm `ScriptExtenderSettings.json` has `"CreateConsole": true` in the `bin\` folder (not the Mods folder).

### Mod not being loaded by SE
- Make sure the `Mods\BG3Bridge\` folder is in `%LocalAppData%\Larian Studios\Baldur's Gate 3\Mods\` not in the Steam game folder.
- Enable the mod from the in-game Mods menu.
- Check the SE console for errors at startup — it will print `[BG3Bridge] Loaded. Listening for DialogStarted events.` if everything is wired up correctly.

### `Ext.Json.Stringify` options
The `{ Beautify = true }` option may not be available in all SE versions. If you see an error in the console about `Stringify`, replace:
```lua
Ext.Json.Stringify(payload, { Beautify = true })
```
with:
```lua
Ext.Json.Stringify(payload)
```

### File path on non-English Windows installations
The `%LocalAppData%` path may differ. You can find the exact Script Extender output directory by running this in the BG3SE Lua console:
```lua
Ext.IO.SaveFile("test.json", "{}")
```
Then search for `test.json` in your AppData folder.

---

## Project File Layout

```
BG3 project/
├── mod/
│   └── Mods/
│       └── BG3Bridge/
│           ├── meta.lsx                      ← BG3 mod metadata
│           └── ScriptExtender/
│               ├── Config.json               ← SE feature flags (Lua enabled)
│               └── Lua/
│                   ├── BootstrapServer.lua   ← Event listener + JSON writer
│                   └── BootstrapClient.lua   ← Placeholder (required)
├── neuro_watcher.py                         ← Python file watcher
└── README.md                                 ← This file
```

---

## Success Criteria Checklist

- [x] SE console prints `[BG3Bridge] Loaded.` on game start.
- [x] Starting any dialogue prints `[BG3Bridge] DialogStarted fired!` in the SE console.
- [x] `neuro_state.json` is created/updated in the Script Extender directory.
- [x] Python watcher detects the change within ~1 second, scrubs the data, and prints it.
- [x] `neuro_raw_events.jsonl` successfully records a rolling history of scrubbed events.

---

## Next Steps (out of scope for this MVP)

- WebSocket relay from the Python watcher to Neuro service.
- Additional events: `Died`, `LeveledUp`, `AreaEntered`, etc.
- Error handling / retry if `neuro_state.json` is locked (partially implemented in watcher).
