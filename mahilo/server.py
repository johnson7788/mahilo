import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets_proxy import Proxy, proxy_connect
from typing import Dict
import uvicorn
import asyncio
import uuid
import logging

import websockets

from rich.console import Console
from rich.traceback import install

## TODO add instructor

from .agent_manager import AgentManager


class ServerManager:
    def __init__(self, agent_manager: AgentManager):
        self.app = FastAPI()
        self.agent_manager = agent_manager
        self.websocket_connections: Dict[str, Dict[str, WebSocket]] = {}
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", None)
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", None)
        self.key = os.getenv("AZURE_OPENAI_KEY", None)
        self.openai_key = os.getenv("OPENAI_API_KEY", None)
        self.proxy_url = os.getenv("PROXY_URL")
        self.token_provider = None

        self.agent_manager.populate_can_contact_for_agents()
        self._setup_routes()

        self.console = Console()
        install()  # This enables rich traceback formatting for exceptions

    def _setup_routes(self):
        @self.app.websocket("/ws/voice-stream/{agent_type}")
        async def voice_stream_endpoint(websocket: WebSocket, agent_type: str):
            await websocket.accept()

            agent = self.agent_manager.get_agent(agent_type)
            if not agent:
                self.console.print(f"[bold red]⛔  Agent type not found:[/bold red] [green]{agent_type}[/green]")
                logging.warning(f"Agent type not found: {agent_type}")
                await websocket.send_text(f"Error: Agent type '{agent_type}' is not registered with the server")
                await websocket.close(1008)  # Using 1008 (Policy Violation) status code
                return
            logging.info("️ New voice stream connection for agent type: " + agent_type)
            self.console.print(
                f"[bold blue]🎙️ New voice stream connection[/bold blue] for agent type: [green]{agent_type}[/green]")

            if not all([self.endpoint, self.deployment, self.key]) and not self.openai_key:
                await websocket.send_text("Azure OpenAI credentials Or OpenaiKey not configured. Voice streaming is unavailable.")
                await websocket.close(1008)  # Using 1008 (Policy Violation) status code
                return

            connection_id = str(uuid.uuid4())

            if agent_type not in self.websocket_connections:
                self.websocket_connections[agent_type] = {}
            self.websocket_connections[agent_type][connection_id] = websocket

            try:
                if self.openai_key:
                    logging.info("OpenaiKey配置成功，使用Openai的配置")
                    headers = (
                        {
                            "Authorization": f"Bearer {self.openai_key}",
                            "openai-beta": "realtime=v1",
                        }
                    )
                    ws_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01"
                else:
                    logging.info(f"Azure OpenAI配置成功，使用Azure OpenAI的配置")
                    headers = (
                        {"api-key": self.key}
                    )
                    ws_url = f"{self.endpoint}/openai/realtime?api-version=2024-10-01-preview&deployment={self.deployment}"
                # add params to the url without using urllib
                # 双向数据流的传输是一直连接
                if self.proxy_url:
                    logging.info("使用代理连接Openai WebSocket")
                    proxy = Proxy.from_url(self.proxy_url)
                    async with proxy_connect(ws_url, extra_headers=headers, proxy=proxy) as openai_ws:
                        print("Connected to Openai WebSocket With Proxy.")
                        await agent._send_session_update(openai_ws)  # 建立最初的连接，然后就保持连接了
                        await asyncio.gather(
                            agent._receive_from_client(websocket, openai_ws),
                            agent._send_to_client(websocket, openai_ws)
                        )
                else:
                    logging.info("直接连接Openai WebSocket")
                    async with websockets.connect(ws_url, extra_headers=headers) as openai_ws:
                        await agent._send_session_update(openai_ws)
                        await asyncio.gather(
                            agent._receive_from_client(websocket, openai_ws),
                            agent._send_to_client(websocket, openai_ws)
                        )
            except WebSocketDisconnect:
                self.console.print(
                    f"[bold yellow]⚠️  WebSocket disconnected[/bold yellow] for agent type: [green]{agent_type}[/green]")
                del self.websocket_connections[agent_type][connection_id]
                if not self.websocket_connections[agent_type]:
                    del self.websocket_connections[agent_type]
            except Exception as e:
                self.console.print(f"[bold red]⛔  Error in voice stream:[/bold red] {str(e)}", style="red")

        @self.app.websocket("/ws/{agent_type}")
        async def websocket_endpoint(websocket: WebSocket, agent_type: str):
            await websocket.accept()

            agent = self.agent_manager.get_agent(agent_type)
            if not agent:
                self.console.print(f"[bold red]⛔  Agent type not found:[/bold red] [green]{agent_type}[/green]")
                await websocket.send_text(f"Error: Agent type '{agent_type}' is not registered with the server")
                await websocket.close(1008)  # Using 1008 (Policy Violation) status code
                return

            self.console.print(
                f"[bold blue]🔌 New WebSocket connection[/bold blue] for agent type: [green]{agent_type}[/green]")

            connection_id = str(uuid.uuid4())

            if agent_type not in self.websocket_connections:
                print(f"Creating new entry for agent type: {agent_type}")
                self.websocket_connections[agent_type] = {}
            self.websocket_connections[agent_type][connection_id] = websocket

            try:
                print(f"Agent retrieved: {agent}")
                while True:
                    data = await websocket.receive_text()
                    self.console.print(
                        f"[dim blue]📨 Received message for agent:[/dim blue] [green]{agent_type}[/green]: [dim]{data}[/dim]")
                    # if the agent is not active, ignore the message
                    if not agent.is_active():
                        self.console.print(
                            f"[bold yellow]⚠️  Agent[/bold yellow] [green]{agent_type}[/green] [bold yellow]is not active[/bold yellow]")
                        await websocket.send_text(f"Agent {agent_type} is not active.")
                        continue
                    response = agent.process_message(data)
                    await websocket.send_text(response["response"])
            except WebSocketDisconnect:
                self.console.print(
                    f"[bold yellow]⚠️  WebSocket disconnected[/bold yellow] for agent type: [green]{agent_type}[/green]")
                del self.websocket_connections[agent_type][connection_id]
                if not self.websocket_connections[agent_type]:
                    print(f"No connections left for agent type: {agent_type}")
                    del self.websocket_connections[agent_type]
            except Exception as e:
                self.console.print(f"[bold red]⛔  Error in websocket:[/bold red] {str(e)}", style="red")

        @self.app.on_event("startup")
        async def startup_event():
            asyncio.create_task(self._handle_inter_agent_communication())

        @self.app.websocket("/health")
        async def health_check(websocket: WebSocket):
            await websocket.accept()
            await websocket.close()

        @self.app.websocket("/ping")
        async def ping_check(websocket: WebSocket):
            await websocket.accept()
            data = await websocket.receive_text()
            print(f"Received message: {data}")
            await websocket.send_text("pong")
            await websocket.close()

        @self.app.api_route("/ping", methods=["GET", "POST"])
        async def root():
            return "Pong"

    async def _handle_inter_agent_communication(self):
        while True:
            for agent in self.agent_manager.get_all_agents():
                if agent.is_active() and agent._queue:
                    message = agent._queue.pop(0)
                    agent_type = agent.TYPE
                    if agent_type in self.websocket_connections:
                        for ws in self.websocket_connections[agent_type].values():
                            self.console.print(
                                f"[bold cyan]📤 Sending inter-agent message[/bold cyan] to websocket: [dim]{ws}[/dim]")
                            ## TODO log to file
                            await ws.send_text(message)
            await asyncio.sleep(1)

    def run(self, host: str = "0.0.0.0", port: int = 5400):
        uvicorn.run(self.app, host=host, port=port)
