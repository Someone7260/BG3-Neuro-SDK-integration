import os
import sys
import json
import time
import re
import ctypes
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any

import mss
import cv2
import numpy as np
import pytesseract

try:
    import pydirectinput
except ImportError:
    pydirectinput = None

# ── Paths & Constants ────────────────────────────────────────────────────────
CONFIG_PATH = Path("bg3_config.json")
STATE_PATH = Path(os.environ.get("LOCALAPPDATA", "")) / r"Larian Studios\Baldur's Gate 3\Script Extender\bg3_state.json"
OUTPUT_PATH = Path("neuro_dialogue_choices.json")

# Regex to match numbered choices: "1. [Persuasion] Leave."
CHOICE_PATTERN = re.compile(r"^\s*(\d+)[\.\)]\s*(.*)", re.IGNORECASE)

# ── Config Loader ────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        print(f"[OCR] ERROR: {CONFIG_PATH} not found. Run bg3_watcher.py to generate defaults.")
        sys.exit(1)
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[OCR] ERROR parsing {CONFIG_PATH}: {exc}")
        sys.exit(1)

def get_dialog_active() -> bool:
    try:
        if STATE_PATH.exists():
            data = json.loads(STATE_PATH.read_bytes())
            return data.get("dialog_active", False)
    except Exception:
        pass
    return False

def is_game_focused() -> bool:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
    return "Baldur's Gate 3" in buff.value

def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S.%f")[:-3]

def main():
    print(f"[{_ts()}] [OCR] Starting BG3 OCR Choice Reader...")
    
    cfg = load_config()
    tesseract_path = cfg.get("tesseract_path", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    
    if not os.path.exists(tesseract_path):
        print(f"[{_ts()}] [OCR] ERROR: Tesseract not found at {tesseract_path}")
        print("Please install Tesseract or update bg3_config.json with the correct path.")
        sys.exit(1)
        
    pytesseract.pytesseract.tesseract_cmd = tesseract_path
    
    poll_interval = cfg.get("ocr_poll_interval_s", 0.5)
    crop_top = cfg.get("ocr_crop_top", 0.65)
    crop_bottom = cfg.get("ocr_crop_bottom", 0.95)
    crop_left = cfg.get("ocr_crop_left", 0.20)
    crop_right = cfg.get("ocr_crop_right", 0.80)
    
    # Subtitle crop defaults (top of the dialogue box)
    sub_crop_top = cfg.get("ocr_subtitle_crop_top", 0.65)
    sub_crop_bottom = cfg.get("ocr_subtitle_crop_bottom", 0.75)
    
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        width, height = monitor["width"], monitor["height"]
        
        box = {
            "top": int(height * crop_top),
            "left": int(width * crop_left),
            "width": int(width * (crop_right - crop_left)),
            "height": int(height * (crop_bottom - crop_top)),
        }
        
        sub_box = {
            "top": int(height * sub_crop_top),
            "left": int(width * crop_left),
            "width": int(width * (crop_right - crop_left)),
            "height": int(height * (sub_crop_bottom - sub_crop_top)),
        }
        
        print(f"[{_ts()}] [OCR] Choice region: {box}")
        print(f"[{_ts()}] [OCR] Subtitle region: {sub_box}")
        
        master_choices = []
        master_subtitle = ""
        empty_frames = 0
        last_dialog_active = False
        
        while True:
            # CRYO FREEZE: If the user alt-tabs out of BG3, completely freeze the script.
            # Do NOT clear the state, otherwise it will aggressively re-scan when they tab back in.
            if not is_game_focused():
                time.sleep(poll_interval)
                continue
                
            active = get_dialog_active()
            
            if not active:
                if last_dialog_active:
                    print(f"[{_ts()}] [OCR] Dialogue closed. Resetting state.")
                master_choices = []
                empty_frames = 0
                last_dialog_active = False
                time.sleep(poll_interval)
                continue
                
            last_dialog_active = True
            
            try:
                # -- WATCHING MODE (Already scanned this node) --
                if master_choices:
                    sct_img = sct.grab(box)
                    img = np.array(sct_img)
                    img = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
                    _, thresh = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
                    text = pytesseract.image_to_string(thresh, config="--psm 6")
                    
                    parts = re.split(r"(?<!\d)([1-9])\s*[\.\)\-]\s*", text)
                    visible_count = len(parts) // 2
                    
                    # If standard threshold missed it (e.g. user hovering mouse causing gold highlight), try normal threshold
                    if visible_count == 0:
                        _, thresh_norm = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
                        text_norm = pytesseract.image_to_string(thresh_norm, config="--psm 6")
                        parts = re.split(r"(?<!\d)([1-9])\s*[\.\)\-]\s*", text_norm)
                        visible_count = len(parts) // 2
                    
                    if visible_count == 0:
                        empty_frames += 1
                        if empty_frames >= 8: # 4 seconds of NO choices -> Node actually changed!
                            print(f"[{_ts()}] [OCR] Node transition detected. Resetting for next scan.")
                            master_choices = []
                            master_subtitle = ""
                            empty_frames = 0
                    else:
                        empty_frames = 0
                    
                    time.sleep(poll_interval)
                    continue

                # -- SCANNING MODE (New node, build the master list) --
                seen_numbers = set()
                scroll_attempts = 0
                
                print(f"[{_ts()}] [OCR] Scanning new dialogue node...")
                
                # 2. OCR the choices with intelligent gap-filling
                def grab_choices():
                    nonlocal master_subtitle
                    sct_img = sct.grab(box)
                    img = np.array(sct_img)
                    img = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
                    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
                    
                    # Dual Thresholding:
                    # Unhighlighted choices are light text on dark background.
                    # Highlighted choices (selected) are dark text on light gold background.
                    # We run both normal and inverted thresholds so Tesseract can perfectly read both!
                    _, thresh_inv = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY_INV)
                    _, thresh_norm = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
                    
                    text_inv = pytesseract.image_to_string(thresh_inv, config="--psm 6")
                    text_norm = pytesseract.image_to_string(thresh_norm, config="--psm 6")
                    
                    for text in [text_inv, text_norm]:
                        parts = re.split(r"(?<!\d)([1-9])\s*[\.\)\-]\s*", text)
                        
                        # Dynamically extract subtitle from the text immediately preceding "1."
                        # This mathematically anchors the subtitle to the choice list, ignoring Y-coordinates entirely!
                        if not master_subtitle and len(parts) > 1 and parts[1] == "1":
                            pre_text = parts[0].strip()
                            lines = [L.strip() for L in pre_text.split('\n') if L.strip()]
                            valid_lines = []
                            for L in lines[-3:]: # Subtitles are at most 2-3 lines. Check the lines right above Choice 1.
                                letters = sum(c.isalpha() for c in L)
                                if len(L) > 0 and (letters / len(L)) > 0.5: # Drop hallucinated environmental garbage
                                    valid_lines.append(L)
                            if valid_lines:
                                clean_sub = " ".join(valid_lines)
                                clean_sub = re.sub(r'^[^a-zA-Z0-9]+|[^a-zA-Z0-9.?!\]\)>\'",\s\-\_]+$', '', clean_sub).strip()
                                # Strip trailing standalone letters (e.g. if OCR hallucinated 'G6.', the 'G' gets pulled into the subtitle)
                                clean_sub = re.sub(r'\s+[A-Za-z]$', '', clean_sub).strip()
                                if len(clean_sub) > 4:
                                    master_subtitle = clean_sub

                        for i in range(1, len(parts), 2):
                            num = int(parts[i])
                            if num not in seen_numbers:
                                content = parts[i+1].strip()
                                content = re.sub(r'[^a-zA-Z0-9.?!\]\)>\'"]+$', '', content).strip()
                                content = content.replace('\n', ' ')
                                # BG3 HACK: The selection highlight completely destroys Tesseract's ability to read the final '8. Leave.' 
                                # It often hallucinates it as garbage (e.g. '& Leave. ; :') and merges it into the previous choice.
                                # If a choice ends with 'Leave' followed by any non-word garbage, we cleanly split it.
                                leave_match = re.search(r'([^\w]*)\b(Leave)[^\w]*$', content, re.IGNORECASE)
                                if leave_match and len(content) > len(leave_match.group(0)) + 5:
                                    text_before = content[:leave_match.start()].strip()
                                    text_before = re.sub(r'[^a-zA-Z0-9.?!\]\)>\'"]+$', '', text_before).strip()
                                    
                                    master_choices.append({"number": num, "text": text_before})
                                    seen_numbers.add(num)
                                    
                                    leave_num = num + 1
                                    master_choices.append({"number": leave_num, "text": "Leave."})
                                    seen_numbers.add(leave_num)
                                else:
                                    master_choices.append({"number": num, "text": content})
                                    seen_numbers.add(num)

                # Pass 1: Top of the list
                grab_choices()
                
                hunt_attempts = 0
                if pydirectinput and seen_numbers:
                    # Pass 2: The Snap (Press 'up' to instantly jump to the bottom)
                    pydirectinput.press('up')
                    time.sleep(0.15)
                    grab_choices()
                    
                    # The Gap Hunt
                    while hunt_attempts < 10:
                        nums_sorted = sorted(list(seen_numbers))
                        if not nums_sorted or nums_sorted[0] != 1:
                            break # Safety abort: if OCR failed to read '1', don't hunt forever
                            
                        # If the length equals the max number, the sequence is perfectly unbroken (1 to N)
                        if len(nums_sorted) == max(nums_sorted):
                            break # No gaps!
                            
                        # Gap detected! Press 'up' repeatedly to force the screen to scroll upward
                        pydirectinput.press('up', presses=5, interval=0.01)
                        time.sleep(0.2)
                        grab_choices()
                        hunt_attempts += 1
                
                if master_choices:
                    master_choices.sort(key=lambda x: x["number"])
                    payload = {
                        "event": "DialogueChoicesRead",
                        "source": "ocr",
                        "status": "done",
                        "timestamp_ms": int(time.time() * 1000),
                        "subtitle": master_subtitle,
                        "choices": master_choices
                    }
                    
                    json_str = json.dumps(payload, indent=2)
                    OUTPUT_PATH.write_text(json_str, encoding="utf-8")
                    print(f"[{_ts()}] [OCR] Finalized {len(master_choices)} choices (via UP-loop). Wrote to {OUTPUT_PATH.name}")
                    
            except Exception as e:
                print(f"[{_ts()}] [OCR] Error during processing: {e}")
                
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
