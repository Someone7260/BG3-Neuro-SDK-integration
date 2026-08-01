import asyncio
import json
import websockets

async def mock_randy(websocket):
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
                    print(f"  - {a.get('name')}")
                    
            elif cmd == "context":
                print(f"\n[MockRandy] === CONTEXT RECEIVED ===")
                print(data.get("message"))
                print(f"====================================")
                
                # Mock Neuro making a decision after 3 seconds
                print("[MockRandy] Neuro is thinking...")
                await asyncio.sleep(3)
                print("[MockRandy] Neuro decides to pick choice 1!")
                
                await websocket.send(json.dumps({
                    "command": "action",
                    "id": "mock_id_123",
                    "name": "select_dialogue",
                    "data": json.dumps({"choice_number": 1})
                }))
                
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
