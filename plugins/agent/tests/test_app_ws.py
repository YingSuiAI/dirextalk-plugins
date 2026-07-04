from fastapi.testclient import TestClient

from dirextalk_agent.app import create_app
from dirextalk_plugins_runtime import AgentPluginSettings


class FakeDirextalkClient:
    pass


class StreamingRuntime:
    async def chat(self, prompt: str, params: dict):
        return {"ok": True, "text": prompt}

    async def stream_chat(self, prompt: str, params: dict):
        yield {"event": "delta", "data": {"text": "hel", "model_profile_id": params.get("model_profile_id")}}
        yield {"event": "done", "data": {"text": "hello"}}


def test_agent_plugin_websocket_stream_frames() -> None:
    app = create_app(settings=AgentPluginSettings(), client=FakeDirextalkClient(), runtime=StreamingRuntime())
    client = TestClient(app)

    with client.websocket_connect("/ws") as websocket:
        websocket.send_json(
            {
                "type": "plugin.invoke.stream",
                "action": "agent.chat.stream",
                "params": {"prompt": "hello", "model_profile_id": "work"},
            }
        )

        delta = websocket.receive_json()
        done = websocket.receive_json()

    assert delta == {
        "type": "plugin.stream.event",
        "event": "delta",
        "data": {"text": "hel", "model_profile_id": "work"},
    }
    assert done == {"type": "plugin.stream.done", "data": {"text": "hello"}}
