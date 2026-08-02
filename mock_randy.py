"""
Mock Randy (Automated Neuro SDK Stand-in)

This script is a Python port/extension of VedalAI's official 'Randy' SDK testing server.
While the official Randy requires manual HTTP POST requests to trigger actions, this 
mock server automatically reads the `actions/force` command and randomly selects a valid 
registered action after a 3-second delay. It is used to fully automate the Phase 5 
closed-loop validation testing without requiring manual intervention.
"""

import asyncio
import json
import websockets

import random
import uuid

async def mock_randy(websocket):
    registered_actions = []
    print(f"[MockRandy] Client connected from {websocket.remote_address}")
    try:
        async for message in websocket:
            data = json.loads(message)
            cmd = data.get("command")
            
            if cmd == "startup":
                print(f"[MockRandy] Startup received for game: {data.get('game')}")
            
            elif cmd == "actions/register":
                actions = data.get("actions", [])
                print(f"[MockRandy] Registered {len(actions)} actions.")
                for a in actions:
                    name = a.get('name')
                    print(f"  - {name}")
                    registered_actions.append(name)
                    
            elif cmd == "actions/unregister":
                unreg_names = data.get("action_names", [])
                print(f"[MockRandy] Unregistered {len(unreg_names)} actions.")
                for name in unreg_names:
                    if name in registered_actions:
                        registered_actions.remove(name)
                    
            elif cmd == "context":
                print(f"\n[MockRandy] === CONTEXT RECEIVED ===")
                print(data.get("message"))
                print(f"====================================")
                
                # Mock Neuro making a decision after 3 seconds
                print("[MockRandy] Neuro is thinking...")
                await asyncio.sleep(3)
                
                if registered_actions:
                    chosen_action = random.choice(registered_actions)
                    print(f"[MockRandy] Neuro decides to pick: {chosen_action}!")
                    
                    await websocket.send(json.dumps({
                        "command": "action",
                        "id": str(uuid.uuid4()),
                        "name": chosen_action,
                        "data": "{}"
                    }))
                else:
                    print("[MockRandy] No actions registered to pick from!")
                
                
            elif cmd == "action/result":
                print(f"[MockRandy] Action result: {data.get('success')} - {data.get('message')}")
                
    except websockets.exceptions.ConnectionClosed:
        print("[MockRandy] Client disconnected.")

async def main():
    print("[MockRandy] Starting mock Neuro SDK server on ws://localhost:8000...")
    async with websockets.serve(mock_randy, "localhost", 8000):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
