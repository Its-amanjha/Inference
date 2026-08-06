"""Bounded notification delivery with periodic session revalidation."""
import asyncio
from typing import Any

import anyio
from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from backend.auth_gate import authenticate, check_origin


class ConnectionManager:
    def __init__(self, max_connections: int=100, queue_size: int=32) -> None:
        self.max_connections=max_connections
        self.queue_size=queue_size
        self._clients: dict[WebSocket,asyncio.Queue[dict[str,Any]]]={}

    async def broadcast(self, event: dict[str,Any]) -> None:
        # Only invalidation metadata crosses the hub. Views fetch authorized detail.
        packet={k:event[k] for k in ('event','instance_id','workflow_id','execution_id','level') if k in event and isinstance(event[k],(str,int,bool))}
        for ws,queue in list(self._clients.items()):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(packet)

    async def handle(self, ws: WebSocket) -> None:
        try:
            check_origin(ws,required=True)
            user=await authenticate(ws)
            if not user:
                await ws.close(code=4401)
                return
        except HTTPException:
            await ws.close(code=4403)
            return
        if len(self._clients)>=self.max_connections:
            await ws.close(code=1013)
            return
        await ws.accept()
        queue: asyncio.Queue[dict[str,Any]]=asyncio.Queue(maxsize=self.queue_size)
        self._clients[ws]=queue
        await ws.send_json({'event':'connected'})

        async def sender() -> None:
            while True:
                try:
                    message=await asyncio.wait_for(queue.get(),timeout=15)
                except asyncio.TimeoutError:
                    message={'event':'heartbeat'}
                if not await authenticate(ws):
                    await ws.close(code=4401)
                    return
                await asyncio.wait_for(ws.send_json(message),timeout=5)

        async def receiver() -> None:
            while True:
                message=await ws.receive()
                if message['type']=='websocket.disconnect':
                    return
                # The notification socket is receive-only for clients.
                if message.get('text') or message.get('bytes'):
                    await ws.close(code=1008)
                    return

        try:
            async with anyio.create_task_group() as group:
                async def run_worker(worker: Any) -> None:
                    try:
                        await worker()
                    except (WebSocketDisconnect,RuntimeError,asyncio.TimeoutError):
                        pass
                    finally:
                        group.cancel_scope.cancel()
                group.start_soon(run_worker,sender)
                group.start_soon(run_worker,receiver)
        finally:
            self._clients.pop(ws,None)

    async def close(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close(code=1001)
            except RuntimeError:
                pass
        self._clients.clear()
