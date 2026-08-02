"""
BG3 Neuro SDK Bridge (Phase 5)

This script acts as the WebSocket client connecting the Baldur's Gate 3 OCR pipeline
to VedalAI's official Neuro SDK. It dynamically registers and unregisters parameter-less 
actions based on on-screen dialogue choices, pushes context, and forces Neuro to make 
a decision using the `actions/force` command.

It also implements a focus-aware async keystroke injector. It safely waits for BG3 
to be the active window, pausing briefly to allow the DirectX input loop to stabilize 
before holding the key, ensuring 100% input reliability.
"""

import asyncio
import json
import os
import pydirectinput
from pathlib import Path
import websockets
import time
import ctypes

NEURO_WS_URL = "ws://localhost:8000"
JSON_PATH = Path("neuro_dialogue_choices.json")

registered_action_names = []

def is_bg3_focused() -> bool:
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buff = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buff, length + 1)
    title = buff.value.strip()
    return title.startswith("Baldur's Gate 3")

async def deferred_keypress(choice: int, action_id: str, websocket):
    timeout = 10.0
    poll = 0.1
    t_start = time.monotonic()
    deferred = False
    
    while True:
        if is_bg3_focused():
            elapsed = time.monotonic() - t_start
            
            # If the user just clicked back into the game, give the engine time to resume input polling
            if deferred:
                await asyncio.sleep(0.5)
            
            pydirectinput.keyDown(str(choice))
            await asyncio.sleep(0.1)
            pydirectinput.keyUp(str(choice))
            
            if deferred:
                print(f"[NeuroBridge] <= Fired keystroke '{choice}' after deferred wait ({elapsed:.1f}s)")
            else:
                print(f"[NeuroBridge] <= Fired keystroke '{choice}' immediately")
                
            await websocket.send(json.dumps({
                "command": "action/result",
                "data": {
                    "id": action_id,
                    "success": True,
                    "message": f"Successfully pressed {choice}."
                }
            }))
            return
            
        elapsed = time.monotonic() - t_start
        if elapsed >= timeout:
            print(f"[NeuroBridge] <= Action dropped — BG3 not focused within {timeout}s")
            await websocket.send(json.dumps({
                "command": "action/result",
                "data": {
                    "id": action_id,
                    "success": False,
                    "message": "Game lost focus; keystroke timed out."
                }
            }))
            return
            
        deferred = True
        await asyncio.sleep(poll)

async def neuro_bridge():
    print(f"[NeuroBridge] Connecting to Neuro SDK at {NEURO_WS_URL}...")
    try:
        async with websockets.connect(NEURO_WS_URL) as websocket:
            print("[NeuroBridge] Connected!")
            
            # 1. Startup
            await websocket.send(json.dumps({
                "command": "startup",
                "data": {
                    "game": "Baldur's Gate 3"
                }
            }))
            
            # Start background task to monitor JSON and send context
            asyncio.create_task(monitor_json(websocket))
            
            # Listen for Actions from Neuro
            async for message in websocket:
                data = json.loads(message)
                if data.get("command") == "action":
                    action_id = data.get("data", {}).get("id")
                    action_name = data.get("data", {}).get("name")
                    action_data = data.get("data", {}).get("data", "{}")
                    
                    if isinstance(action_data, str):
                        try:
                            action_data = json.loads(action_data)
                        except:
                            action_data = {}
                            
                    if action_name and action_name.startswith("choose_option_"):
                        try:
                            choice = int(action_name.split("_")[-1])
                        except ValueError:
                            choice = None
                            
                        print(f"\n[NeuroBridge] => NEURO COMMANDED: {action_name}")
                        if choice is not None:
                            # Fire focus-aware keystroke task
                            asyncio.create_task(deferred_keypress(choice, action_id, websocket))
                            
                            # Mark local file as processed so we don't re-send the same context
                            try:
                                jdata = json.loads(JSON_PATH.read_text(encoding="utf-8"))
                                jdata["status"] = "processed"
                                JSON_PATH.write_text(json.dumps(jdata, indent=2), encoding="utf-8")
                            except: pass
                        else:
                            await websocket.send(json.dumps({
                                "command": "action/result",
                                "data": {
                                    "id": action_id,
                                    "success": False,
                                    "message": "Invalid choice format."
                                }
                            }))

    except Exception as e:
        print(f"[NeuroBridge] Connection error: {e}")
        print("[NeuroBridge] Retrying in 5 seconds...")
        await asyncio.sleep(5)
        await neuro_bridge()


async def monitor_json(websocket):
    global registered_action_names
    last_timestamp = 0
    print(f"[NeuroBridge] Monitoring {JSON_PATH.name} for new dialogues...")
    while True:
        try:
            if JSON_PATH.exists():
                text = JSON_PATH.read_text(encoding="utf-8")
                if text.strip():
                    data = json.loads(text)
                    if data.get("status") == "done" and data.get("timestamp_ms", 0) != last_timestamp:
                        last_timestamp = data.get("timestamp_ms", 0)
                        
                        subtitle = data.get("subtitle", "")
                        choices = data.get("choices", [])
                        
                        # Gap validation
                        nums = sorted([c.get("number") for c in choices if c.get("number") is not None])
                        if not nums or nums[0] != 1 or len(nums) != max(nums):
                            print(f"[NeuroBridge] Choices have gaps or start > 1: {nums}. Skipping registration.")
                            continue
                        
                        # Unregister old actions
                        if registered_action_names:
                            await websocket.send(json.dumps({
                                "command": "actions/unregister",
                                "data": {
                                    "action_names": registered_action_names
                                }
                            }))
                            print(f"[NeuroBridge] Unregistered old actions: {registered_action_names}")
                            registered_action_names = []
                            
                        # Register new parameter-less actions
                        new_actions = []
                        for c in choices:
                            name = f"choose_option_{c['number']}"
                            new_actions.append({
                                "name": name,
                                "description": c['text'],
                                "schema": {
                                    "type": "object",
                                    "properties": {}
                                }
                            })
                            registered_action_names.append(name)
                            
                        if new_actions:
                            await websocket.send(json.dumps({
                                "command": "actions/register",
                                "data": {
                                    "actions": new_actions
                                }
                            }))
                            print(f"[NeuroBridge] Registered {len(new_actions)} actions dynamically.")
                        
                        # Send context separately
                        context_str = f"NPC says: \"{subtitle}\"\n\nAvailable Responses:\n"
                        for c in choices:
                            context_str += f"{c['number']}. {c['text']}\n"
                            
                        print(f"\n[NeuroBridge] <= Pushing new Context to Neuro:\n{context_str}")
                        
                        await websocket.send(json.dumps({
                            "command": "context",
                            "data": {
                                "message": context_str
                            }
                        }))
                        
                        # Force Neuro to make a decision immediately
                        if registered_action_names:
                            await websocket.send(json.dumps({
                                "command": "actions/force",
                                "data": {
                                    "state": "Dialogue",
                                    "query": "What do you say?",
                                    "ephemeral_context": False,
                                    "action_names": registered_action_names
                                }
                            }))
                            print(f"[NeuroBridge] <= Forced actions: {registered_action_names}")
                        
        except Exception as e:
            pass
        
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    asyncio.run(neuro_bridge())
