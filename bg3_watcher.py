"""
bg3_watcher.py
==============
Phase 3 Hardened — BG3 Bridge event watcher.

Two independent daemon threads:
  - FileWatcher : polls bg3_state.json, parses with retry+backoff,
                  dedups by sequence_id, scrubs, logs to JSONL, enqueues.
  - WSRelay     : maintains WebSocket with exponential backoff + heartbeat,
                  flushes event queue to the AI endpoint.

Failure in either thread cannot cascade to the other.
The JSONL log is always written regardless of WebSocket state.

USAGE
-----
    python bg3_watcher.py
    python bg3_supervisor.py     # recommended for production (auto-restarts)

CONFIG
------
    bg3_config.json  (auto-created with defaults on first run)
"""

import os, sys, json, time, threading, argparse
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Optional

# ── Optional actuation dependencies ──────────────────────────────────────────
try:
    import win32gui
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import pydirectinput
    HAS_PYDIRECTINPUT = True
except ImportError:
    HAS_PYDIRECTINPUT = False

# SDL_app is the stable window class for BG3 regardless of resolution/API.
BG3_WINDOW_CLASS = "SDL_app"

# ── Optional dependencies ────────────────────────────────────────────────────
try:
    from watchfiles import watch as wf_watch   # type: ignore
    HAS_WATCHFILES = True
except ImportError:
    HAS_WATCHFILES = False

try:
    import websockets   # type: ignore
    import asyncio
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

# ── ANSI colours ─────────────────────────────────────────────────────────────
CYAN = "\033[96m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
RED  = "\033[91m"; RESET = "\033[0m";  BOLD   = "\033[1m"

# ── Paths & constants ────────────────────────────────────────────────────────
DEFAULT_WATCH_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / (
    r"Larian Studios\Baldur's Gate 3\Script Extender\bg3_state.json"
)
ALLOWED_KEYS = {
    "last_event", "last_event_timestamp", "sequence_id",
    "dialog_active", "dialog_name", "dialog_instanceID", "dialog_actors",
    "dialog_node_uuid", "combat_active", "combat_guid",
    "event", "source", "timestamp_ms", "choices",
}
CONFIG_PATH  = Path("bg3_config.json")
OP_LOG_PATH  = Path("bg3_watcher.log")
RAW_LOG_PATH = Path("bg3_raw_events.jsonl")
DEFAULT_CONFIG = {
    "poll_interval_s": 0.25,
    "ws_uri": "ws://localhost:8765",
    "ws_enabled": True,
    "ws_reconnect_backoff_initial_s": 1,
    "ws_reconnect_backoff_max_s": 30,
    "ws_heartbeat_interval_s": 15,
    "ws_heartbeat_timeout_s": 5,
    "ws_relay_queue_max_size": 100,
    "log_rotation_max_bytes": 5_242_880,   # 5 MB
    "log_rotation_max_files": 5,
    "no_watchfiles": False,
    # Actuation layer
    "actuation_enabled": True,
    "actuation_delay_s": 5,
    "actuation_focus_wait_timeout_s": 10,  # Drop the action if BG3 not focused within this
    "actuation_focus_poll_interval_s": 0.25,
    # OCR settings
    "tesseract_path": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    "ocr_crop_top": 0.65,
    "ocr_crop_bottom": 0.95,
    "ocr_crop_left": 0.2,
    "ocr_crop_right": 0.8,
    "ocr_poll_interval_s": 0.5,
}


# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"{YELLOW}[CONFIG] Could not load {CONFIG_PATH}: {exc}. Using defaults.{RESET}")
    else:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
        print(f"{CYAN}[CONFIG] Created default config at {CONFIG_PATH}{RESET}")
    return cfg


# ── Operational logger (separate from game-data JSONL) ───────────────────────
_op_lock = threading.Lock()

def op_log(level: str, msg: str) -> None:
    """Write operational events to bg3_watcher.log — never to bg3_raw_events.jsonl."""
    line = f"[{_ts()}] [{level.upper():5}] {msg}"
    with _op_lock:
        with open(OP_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    if level.upper() in ("WARN", "ERROR"):
        print(f"{RED if level.upper() == 'ERROR' else YELLOW}{line}{RESET}")


# ── Raw event logger with size-based rotation ─────────────────────────────────
_raw_lock = threading.Lock()

def log_raw_event(data: dict, bridge_ts: str, cfg: dict) -> None:
    """Append scrubbed event to bg3_raw_events.jsonl, rotating when size exceeded."""
    record = {**data, "_watcher_ts": bridge_ts}
    line   = json.dumps(record) + "\n"
    with _raw_lock:
        if RAW_LOG_PATH.exists() and RAW_LOG_PATH.stat().st_size >= cfg["log_rotation_max_bytes"]:
            _rotate_raw_log(cfg)
        with open(RAW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)

def _rotate_raw_log(cfg: dict) -> None:
    stamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = RAW_LOG_PATH.with_name(f"bg3_raw_events.{stamp}.jsonl")
    RAW_LOG_PATH.rename(archive)
    op_log("INFO", f"Rotated raw log → {archive.name}")
    archives = sorted(RAW_LOG_PATH.parent.glob("bg3_raw_events.????????_??????.jsonl"))
    while len(archives) > cfg["log_rotation_max_files"]:
        oldest = archives.pop(0)
        oldest.unlink()
        op_log("INFO", f"Pruned old archive: {oldest.name}")


# ── Misc helpers ──────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]

def banner(msg: str, color: str = CYAN) -> None:
    w = 72
    print(f"{color}{BOLD}{'─' * w}{RESET}")
    print(f"{color}{BOLD}  {msg}{RESET}")
    print(f"{color}{BOLD}{'─' * w}{RESET}")

def scrub(data: dict) -> dict:
    return {k: v for k, v in data.items() if k in ALLOWED_KEYS}

def print_event(data: dict, bridge_ts: str) -> None:
    event_name = data.get('last_event') or data.get('event', '?')
    seq = data.get('sequence_id', '?')
    banner(f"[{bridge_ts}]  BG3 EVENT RECEIVED", GREEN)
    
    seq_str = f"  [seq #{seq}]" if seq != '?' else ""
    print(f"  {BOLD}event     {RESET}: {event_name}{seq_str}")
    
    if "choices" in data:
        print(f"  {BOLD}choices   {RESET}: {len(data['choices'])} detected")
        for choice in data["choices"]:
            print(f"    {choice['number']}. {choice['text']}")
        print()
        return

    dlg = data.get("dialog_active", False)
    print(f"  {BOLD}dialogue  {RESET}: {'active' if dlg else 'inactive'}", end="")
    if dlg:
        actors = data.get("dialog_actors", [])
        print(f" | {', '.join(actors) if actors else '(no actors yet)'}", end="")
    print()
    cbt = data.get("combat_active", False)
    print(f"  {BOLD}combat    {RESET}: {'🔴 ACTIVE' if cbt else '⚪ off'}")
    print(f"  {BOLD}game_ms   {RESET}: {data.get('last_event_timestamp', data.get('timestamp_ms', 'N/A'))}")
    print(f"  {BOLD}bridge_ts {RESET}: {bridge_ts}")
    print()


# ── Thread-safe bounded event queue (drop-oldest on overflow) ─────────────────
class EventQueue:
    def __init__(self, maxsize: int):
        self._dq:   deque = deque(maxlen=maxsize)
        self._lock: threading.Lock = threading.Lock()

    def push(self, item: dict) -> bool:
        """Returns True if an old item was dropped to make room."""
        with self._lock:
            dropped = len(self._dq) >= (self._dq.maxlen or 0)
            if dropped:
                self._dq.popleft()
            self._dq.append(item)
        return dropped

    def drain(self) -> list:
        with self._lock:
            items = list(self._dq)
            self._dq.clear()
        return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._dq)


# ── Actuation helpers ────────────────────────────────────────────────────────
def _is_bg3_focused() -> bool:
    """Return True if the foreground window belongs to BG3 (class == SDL_app)."""
    if not HAS_WIN32:
        return False
    try:
        hwnd = win32gui.GetForegroundWindow()
        return win32gui.GetClassName(hwnd) == BG3_WINDOW_CLASS
    except Exception:
        return False


def _send_key(key: str) -> None:
    """Send a DirectInput keystroke. Falls back to a log warning if unavailable."""
    if HAS_PYDIRECTINPUT:
        pydirectinput.press(key)
    else:
        op_log("WARN", "pydirectinput not available — keystroke not sent.")


# ── File Watcher ──────────────────────────────────────────────────────────────
class FileWatcher:
    def __init__(self, path: Path, cfg: dict, eq: EventQueue):
        self.path = path
        self.cfg  = cfg
        self.eq   = eq
        self._last_seq:        int            = -1
        self._last_mtime:      float          = 0.0
        self._last_size:       int            = 0
        self._consec_fails:    int            = 0
        self._current_dialog_id: Optional[str] = None  # tracks active dialogue
        self._pending_action_id: Optional[str] = None  # dialog_instanceID that spawned pending action
        self._pending_lock:    threading.Lock = threading.Lock()

    # ── Parse with retry + backoff ────────────────────────────────────────────
    def _parse(self, bridge_ts: str) -> Optional[dict]:
        raw = b""
        for attempt in range(3):
            try:
                raw  = self.path.read_bytes()
                data = json.loads(raw)
                
                self._consec_fails = 0
                return data
            except (json.JSONDecodeError, OSError):
                if attempt < 2:
                    time.sleep(0.05)

        self._consec_fails += 1
        snippet = raw[:512].decode("utf-8", errors="replace")
        op_log("WARN", f"Parse failed #{self._consec_fails} at {bridge_ts}. Raw snippet: {snippet!r}")

        if self._consec_fails >= 3:
            sleep_s = min(0.5 * 2 ** (self._consec_fails - 3), 5.0)
            op_log("WARN", f"Consecutive failures, backing off {sleep_s:.1f}s")
            time.sleep(sleep_s)
        return None

    # ── Deferred focus-aware keystroke ────────────────────────────────────────
    def _deferred_keystroke(self, dialog_id: str, key: str) -> None:
        """Run in a daemon thread. Fires the keystroke once BG3 is focused,
        or drops the action if the timeout elapses or the dialogue context changes."""
        timeout  = self.cfg["actuation_focus_wait_timeout_s"]
        poll     = self.cfg["actuation_focus_poll_interval_s"]
        t_start  = time.monotonic()
        deferred = False

        while True:
            # Stale-context guard: cancel if dialogue moved on.
            with self._pending_lock:
                if self._pending_action_id != dialog_id:
                    op_log("WARN", f"Pending action cancelled — dialogue context changed before focus returned. (was {dialog_id})")
                    return

            if _is_bg3_focused():
                elapsed = time.monotonic() - t_start
                _send_key(key)
                if deferred:
                    op_log("INFO", f"Actuation: action fired after deferred wait ({elapsed:.1f}s). key='{key}' dialog={dialog_id}")
                else:
                    op_log("INFO", f"Actuation: action fired immediately. key='{key}' dialog={dialog_id}")
                with self._pending_lock:
                    if self._pending_action_id == dialog_id:
                        self._pending_action_id = None
                return

            elapsed = time.monotonic() - t_start
            if elapsed >= timeout:
                op_log("WARN", f"Actuation: action dropped — game not focused within {timeout}s timeout. dialog={dialog_id}")
                with self._pending_lock:
                    if self._pending_action_id == dialog_id:
                        self._pending_action_id = None
                return

            deferred = True
            time.sleep(poll)

    def _schedule_keystroke(self, dialog_id: str, key: str, delay_s: float) -> None:
        """Wait delay_s seconds (the dialogue-open delay), then enter the deferred loop."""
        time.sleep(delay_s)
        self._deferred_keystroke(dialog_id, key)

    # ── Process one file-change event ─────────────────────────────────────────
    def _process(self, bridge_ts: str) -> None:
        data = self._parse(bridge_ts)
        if data is None:
            return

        clean = scrub(data)
        seq   = clean.get("sequence_id", -1)

        # Dedup / gap detection
        if seq != -1:
            if seq == self._last_seq:
                op_log("DEBUG", f"Dup seq={seq}, skipping.")
                return
            if self._last_seq != -1:
                if seq < self._last_seq - 5:
                    op_log("INFO", f"Seq reset {self._last_seq}→{seq}: new game session.")
                    self._last_seq = -1
                elif seq > self._last_seq + 1:
                    op_log("WARN", f"Seq gap: expected {self._last_seq + 1}, got {seq}. Events dropped?")
            self._last_seq = seq

        log_raw_event(clean, bridge_ts, self.cfg)
        if self.eq.push(clean):
            op_log("WARN", "Relay queue full — dropped oldest event.")
        print_event(clean, bridge_ts)

        # ── Actuation trigger ─────────────────────────────────────────────────
        if self.cfg.get("actuation_enabled", True) and (HAS_WIN32 and HAS_PYDIRECTINPUT):
            dialog_active = clean.get("dialog_active", False)
            dialog_id     = clean.get("dialog_instanceID", "")

            if dialog_active and dialog_id and dialog_id != self._current_dialog_id:
                # New dialogue detected — arm a pending action.
                self._current_dialog_id = dialog_id
                with self._pending_lock:
                    self._pending_action_id = dialog_id
                delay = self.cfg.get("actuation_delay_s", 5)
                op_log("INFO", f"Actuation: new dialogue {dialog_id} — keystroke '1' scheduled in {delay}s.")
                t = threading.Thread(
                    target=self._schedule_keystroke,
                    args=(dialog_id, "1", delay),
                    name=f"Actuator-{dialog_id}",
                    daemon=True,
                )
                t.start()

            elif not dialog_active and "choices" not in clean:
                # Dialogue ended — reset tracking and cancel any pending action.
                if self._current_dialog_id is not None:
                    with self._pending_lock:
                        if self._pending_action_id == self._current_dialog_id:
                            self._pending_action_id = None  # stale guard in thread will catch this
                    self._current_dialog_id = None

            if "choices" in clean and len(clean["choices"]) > 1:
                target_key = "2" # Hardcoded for Phase 4 proof-of-concept
                # We use a fake dialog_id "ocr_test" just to pass the stale guard.
                test_id = "ocr_test"
                with self._pending_lock:
                    self._pending_action_id = test_id
                op_log("INFO", f"Actuation: OCR read {len(clean['choices'])} choices. Firing '{target_key}' test harness.")
                t = threading.Thread(
                    target=self._schedule_keystroke,
                    args=(test_id, target_key, 0.5),
                    name="Actuator-OCR",
                    daemon=True,
                )
                t.start()

    # ── Poll loop (no watchfiles) ─────────────────────────────────────────────
    def run_poll(self) -> None:
        interval = self.cfg["poll_interval_s"]
        _dir_warned = _file_warned = False
        op_log("INFO", f"FileWatcher started (poll, interval={interval}s)")

        while True:
            try:
                if not self.path.parent.exists():
                    if not _dir_warned:
                        print(f"{YELLOW}[{_ts()}] BG3SE output dir not found — launch BG3 first.{RESET}")
                        _dir_warned = True
                    time.sleep(interval); continue
                _dir_warned = False

                st = self.path.stat()
                if st.st_mtime != self._last_mtime or st.st_size != self._last_size:
                    self._last_mtime, self._last_size = st.st_mtime, st.st_size
                    _file_warned = False
                    self._process(_ts())

            except FileNotFoundError:
                if not _file_warned:
                    print(f"{YELLOW}[{_ts()}] Waiting for bg3_state.json — start a BG3 dialogue.{RESET}")
                    _file_warned = True
            except Exception as exc:
                op_log("ERROR", f"FileWatcher error: {exc}")

            time.sleep(interval)

    # ── watchfiles loop ───────────────────────────────────────────────────────
    def run_watchfiles(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        op_log("INFO", "FileWatcher started (watchfiles mode)")
        print(f"{CYAN}watchfiles backend active → {self.path}{RESET}\n")
        for changes in wf_watch(str(self.path.parent)):
            for _, changed_path in changes:
                if Path(changed_path).name == self.path.name:
                    self._process(_ts())


# ── WebSocket Relay ───────────────────────────────────────────────────────────
class WSRelay:
    def __init__(self, cfg: dict, eq: EventQueue):
        self.cfg = cfg
        self.eq  = eq

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._relay_loop())
        finally:
            loop.close()

    async def _relay_loop(self) -> None:
        backoff = self.cfg["ws_reconnect_backoff_initial_s"]
        uri     = self.cfg["ws_uri"]
        while True:
            try:
                op_log("INFO", f"WSRelay connecting → {uri}")
                async with websockets.connect(uri, ping_interval=None) as ws:
                    op_log("INFO", "WSRelay connected.")
                    backoff = self.cfg["ws_reconnect_backoff_initial_s"]
                    await self._session(ws)
            except Exception as exc:
                op_log("WARN", f"WSRelay disconnected: {exc}. Retry in {backoff}s.")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.cfg["ws_reconnect_backoff_max_s"])

    async def _session(self, ws) -> None:
        hb_iv  = self.cfg["ws_heartbeat_interval_s"]
        hb_to  = self.cfg["ws_heartbeat_timeout_s"]
        last_hb = time.monotonic()

        while True:
            for evt in self.eq.drain():
                await ws.send(json.dumps(evt))

            if time.monotonic() - last_hb >= hb_iv:
                try:
                    pong_waiter = await ws.ping()
                    await asyncio.wait_for(asyncio.shield(pong_waiter), timeout=hb_to)
                    last_hb = time.monotonic()
                except asyncio.TimeoutError:
                    op_log("WARN", "WSRelay heartbeat timeout — forcing reconnect.")
                    raise ConnectionError("Heartbeat timeout")

            await asyncio.sleep(0.1)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="BG3 Bridge Phase 3 Watcher")
    parser.add_argument("--path",          type=str, default=None,
                        help="Path to bg3_state.json (overrides config default).")
    parser.add_argument("--no-watchfiles", action="store_true",
                        help="Force polling mode even if watchfiles is installed.")
    args = parser.parse_args()

    cfg = load_config()
    if args.no_watchfiles:
        cfg["no_watchfiles"] = True

    watch_path = Path(args.path) if args.path else DEFAULT_WATCH_PATH

    banner("BG3 Bridge Watcher  —  Phase 3 v0.3")
    print(f"  Watching   : {watch_path}")
    print(f"  Started    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Python     : {sys.version.split()[0]}")
    ws_status = (f"enabled → {cfg['ws_uri']}" if HAS_WEBSOCKETS
                 else "disabled (install: pip install websockets)")
    print(f"  WebSocket  : {ws_status if cfg.get('ws_enabled') else 'disabled in config'}")
    print(f"  watchfiles : {'available' if HAS_WATCHFILES and not cfg['no_watchfiles'] else 'polling'}")
    print()
    print(f"{YELLOW}Trigger a dialogue in BG3 to test. Press Ctrl+C to stop.{RESET}\n")
    op_log("INFO", "=== BG3 Bridge Watcher started ===")

    eq = EventQueue(maxsize=cfg["ws_relay_queue_max_size"])
    fw = FileWatcher(watch_path, cfg, eq)
    
    ocr_path = Path("neuro_dialogue_choices.json")
    fw_ocr = FileWatcher(ocr_path, cfg, eq)

    # WS relay thread (independent failure domain)
    if cfg.get("ws_enabled", True) and HAS_WEBSOCKETS:
        ws_thread = threading.Thread(target=WSRelay(cfg, eq).run, name="WSRelay", daemon=True)
        ws_thread.start()

    # File watcher thread (Game events)
    use_wf   = HAS_WATCHFILES and not cfg["no_watchfiles"]
    fw_target = fw.run_watchfiles if use_wf else fw.run_poll
    fw_thread = threading.Thread(target=fw_target, name="FileWatcher", daemon=True)
    fw_thread.start()
    
    # File watcher thread (OCR events)
    fw_ocr_target = fw_ocr.run_watchfiles if use_wf else fw_ocr.run_poll
    fw_ocr_thread = threading.Thread(target=fw_ocr_target, name="FileWatcherOCR", daemon=True)
    fw_ocr_thread.start()

    # Main thread — block until Ctrl-C
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        op_log("INFO", "=== BG3 Bridge Watcher stopped by user ===")
        print(f"\n{YELLOW}BG3 Bridge watcher stopped.{RESET}")


if __name__ == "__main__":
    main()
