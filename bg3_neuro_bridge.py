import asyncio
import json
import os
import pydirectinput
from pathlib import Path
import websockets

NEURO_WS_URL = "ws://localhost:8000"
JSON_PATH = Path("neuro_dialogue_choices.json")

async def neuro_bridge():
    print(f"[NeuroBridge] Connecting to Neuro SDK at {NEURO_WS_URL}...")
    try:
        async with websockets.connect(NEURO_WS_URL) as websocket:
            print("[NeuroBridge] Connected!")
            
            # 1. Startup
            await websocket.send(json.dumps({
                "command": "startup",
                "game": "Baldur's Gate 3"
            }))
            
            # 2. Register Actions
            await websocket.send(json.dumps({
                "command": "actions/register",
                "actions": [
                    {
                        "name": "select_dialogue",
                        "description": "Selects a dialogue choice based on the given number.",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "choice_number": { "type": "integer", "minimum": 1, "maximum": 9 }
                            },
                            "required": ["choice_number"]
                        }
                    }
                ]
            }))
            print("[NeuroBridge] Registered action: select_dialogue")

            # Start background task to monitor JSON and send context
            asyncio.create_task(monitor_json(websocket))
            
            # 3. Listen for Actions from Neuro
            async for message in websocket:
                data = json.loads(message)
                if data.get("command") == "action":
                    action_id = data.get("id")
                    action_name = data.get("name")
                    action_data = data.get("data", "{}")
                    
                    if isinstance(action_data, str):
                        try:
                            action_data = json.loads(action_data)
                        except:
                            action_data = {}
                            
                    if action_name == "select_dialogue":
                        choice = action_data.get("choice_number")
                        print(f"\n[NeuroBridge] => NEURO COMMANDED: select_dialogue({choice})")
                        if choice is not None:
                            # Actuate the key press!
                            pydirectinput.press(str(choice))
                            print(f"[NeuroBridge] <= Fired keystroke '{choice}' into BG3!")
                            
                            # Reply with success
                            await websocket.send(json.dumps({
                                "command": "action/result",
                                "id": action_id,
                                "success": True,
                                "message": f"Successfully pressed {choice}."
                            }))
                            
                            # Mark local file as processed so we don't re-send the same context
                            try:
                                jdata = json.loads(JSON_PATH.read_text(encoding="utf-8"))
                                jdata["status"] = "processed"
                                JSON_PATH.write_text(json.dumps(jdata, indent=2), encoding="utf-8")
                            except: pass
                        else:
                            await websocket.send(json.dumps({
                                "command": "action/result",
                                "id": action_id,
                                "success": False,
                                "message": "Missing choice_number."
                            }))

    except Exception as e:
        print(f"[NeuroBridge] Connection error: {e}")
        print("[NeuroBridge] Retrying in 5 seconds...")
        await asyncio.sleep(5)
        await neuro_bridge()


async def monitor_json(websocket):
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
                        
                        context_str = f"NPC says: \"{subtitle}\"\n\nAvailable Responses:\n"
                        for c in choices:
                            context_str += f"{c['number']}. {c['text']}\n"
                            
                        print(f"\n[NeuroBridge] <= Pushing new Context to Neuro:\n{context_str}")
                        
                        # Send context
                        await websocket.send(json.dumps({
                            "command": "context",
                            "message": context_str
                        }))
        except Exception as e:
            pass
        
        await asyncio.sleep(0.5)

if __name__ == "__main__":
    asyncio.run(neuro_bridge())
