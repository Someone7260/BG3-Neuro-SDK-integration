# BG3-Neuro-SDK-Integration

This repository contains a working end-to-end prototype telemetry and actuation pipeline bridging Baldur's Gate 3 with VedalAI's Neuro SDK. It allows an external AI client to read in-game dialogue state, parse dialogue choices from the screen, and inject keystrokes to control the game — demonstrated with a full successful loop against the SDK's reference test client (Randy).

Baldur's Gate 3 has no first-party support for external control or telemetry. Building a working bridge required working through several real, non-obvious engine and modding-ecosystem obstacles, documented honestly below rather than glossed over.

---

## 🏗️ Architecture Pipeline

The system is split into four components:

### 1. The Engine Hook (Lua Sandbox Mod)
Uses the BG3 Script Extender (BG3SE), a third-party tool, to hook directly into the game's Osiris event system (`DialogStarted`, `DialogActorJoined`, `EnteredCombat`, `DialogRollResult`, and others). BG3SE's Lua sandbox does not have a working networking library, so captured state is serialized and written to a local file (`bg3_raw_events.jsonl`) via atomic writes, rather than sent over a socket directly from Lua.

### 2. The Supervisor & Security Scrubber (`bg3_supervisor.py`)
A real-time Python service tails the Lua output file, with several independent responsibilities:
- Scrubs hardware paths, Windows usernames, and Steam IDs before any data leaves the local machine.
- Filters engine noise (e.g. inanimate objects like torches or chests incorrectly triggering combat-adjacent events) via an `Osi.IsCharacter` check.
- Deduplicates and sequence-checks events to catch dropped or repeated writes.
- Supervises the watcher process itself, correctly detecting force-killed processes (which can falsely report a clean exit code) and restarting them, with a crash-loop cap.

### 3. The Computer Vision Pipeline (`bg3_ocr.py`)
BG3SE does not currently expose reliable read access to the game's live UI data (the UI is built on NoesisGUI, a WPF-style framework, and BG3SE's binding to it does not support general traversal of arbitrary UI elements at this time). Rather than continuing to pursue that path, dialogue choice text is read directly from the screen via OCR:
- Captures a configurable screen region (percentage-based, not hardcoded pixels) only while a dialogue is active.
- Applies dual-threshold preprocessing so choice text is read correctly whether or not it's currently highlighted by mouse hover.
- Dynamically locates the numbered choice list to extract the preceding subtitle line, rather than relying on fixed coordinates that break when the dialogue box resizes.
- Detects and fills gaps in the numbered sequence when a choice list exceeds the visible screen area.

This approach has known, documented limitations: OCR occasionally introduces small artifacts on certain lines (e.g. a stray trailing character, or a truncated leading bracket on some choices). These have been observed in testing and are noted as an open item rather than papered over.

### 4. The SDK Actuation Bridge (`bg3_neuro_bridge.py`)
An asynchronous WebSocket client implementing the Neuro Game SDK protocol:
- Sends the `startup` command on connect.
- Translates the OCR'd subtitle and choice list into a `context` command.
- Dynamically registers one action per currently visible choice (`choose_option_1`, `choose_option_2`, etc.) via `actions/register`, and unregisters the previous set before registering a new one whenever the dialogue state changes — preventing stale actions from a previous conversation from remaining selectable.
- Uses `actions/force` to prompt a decision when the game is paused on a dialogue choice.
- On receiving an `action` command, maps it back to the corresponding numbered choice and fires a keystroke via `PyDirectInput`/`SendInput`, then reports the outcome via `action/result`.

---

## 🧗 Technical Challenges & Solutions

Full history in `dev_logs/`. The two hardest problems, and what actually solved them:

### Deploying a Script Extender Mod Under Patch 7
Patch 7 introduced a stricter in-game Mod Manager that validates loaded mods against `modsettings.lsx`, which created a real deployment obstacle specific to BG3SE-based mods:

- Exporting through the official Larian Toolkit did not reliably preserve the mod's `ScriptExtender/` folder — the Toolkit's own export pipeline isn't built with third-party tools like BG3SE in mind.
- An intermediate approach (Toolkit export, inject Lua via `divine.exe`, manually patch the resulting file's hash in `modsettings.lsx`) was tried and failed: the hand-edited hash triggered Patch 7's file-integrity check, and the mod was silently disabled.
- A symlink-based hash-spoofing workaround was considered and explicitly rejected — incompatible with shipping something clean and stream-safe.
- **The actual root cause**, found after a full clean-slate rebuild: a UTF-8 byte-order-mark (BOM) in the mod's `Config.json` and `BootstrapServer.lua`, silently breaking both BG3SE's JSON parser and its Lua parser. Re-saving these files as UTF-8 without BOM resolved it completely — the mod then loaded reliably via the Toolkit's standard loose-file dev deployment path (`Data\Mods\<ModName>`), with no hash editing or workarounds needed at all.
- Verified stable across repeated relaunches and toggling the mod via the in-game Mods menu.

BG3 Mod Manager (BG3MM) was investigated as a documented alternative packaging tool for BG3SE mods, but the encoding fix above — not a switch to BG3MM — is what actually resolved deployment in this project.

### Focus-Aware Keystroke Delivery
Simulated keystrokes only register with the game while its window holds OS foreground focus. This produced real, observed failures during testing — for example, a Randy-driven run correctly returned `"Game lost focus; keystroke timed out."` via `action/result` rather than silently reporting success.

- The bridge checks foreground focus before firing a keystroke. If BG3 doesn't have focus, the action is deferred and polled for up to a bounded timeout, rather than fired blindly or forced via `SetForegroundWindow` (unreliable due to Windows' focus-steal prevention, and disruptive on a live stream regardless).
- If the dialogue state changes while an action is deferred, it's cancelled rather than fired late into a context it no longer applies to.
- This does not guarantee unconditional success — an unfocused window will still correctly fail the action — but it fails safely and reports accurately, which is the property that matters for a stream-facing tool.

### Stale Context in the Neuro SDK
Persistent, hardcoded actions led to the AI occasionally being offered choices no longer on screen.
- **Solution:** `actions/unregister` is issued the moment the game state changes, before the new OCR'd choices are pushed as fresh, dynamic, parameter-less actions.
- `actions/force` is used to prompt a decision whenever a dialogue pauses the game, since Neuro's SDK does not proactively act without being prompted.

---

## ✅ Verified Result

A full loop has been demonstrated end-to-end against the SDK reference test client (Randy): a real in-game dialogue was read via OCR, its choices registered as SDK actions, a decision received, the corresponding keystroke fired, and success confirmed via `action/result` — with a separate, correctly-reported focus failure observed in an earlier run, confirming the error path works as designed, not just the happy path.

---

## 🚀 Execution & Setup

1. **Neuro SDK** — start the official SDK WebSocket server, or `mock_randy.py` for automated loop testing.
2. **Neuro Bridge** — `python bg3_neuro_bridge.py`
3. **BG3 Supervisor** — `python bg3_supervisor.py`
4. **OCR Engine** — `python bg3_ocr.py`
5. **Baldur's Gate 3** — launch normally with the mod placed in `Data\Mods\<ModName>` per the Toolkit's loose-file dev deployment method. Borderless Windowed mode recommended for reliable focus detection.