import json

import httpx
from openai import OpenAI

from wactx.config import Config
from wactx.search import expand_query


def _client(handler):
    return OpenAI(
        api_key="test",
        base_url="http://mock/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok(body, content='["alt one", "alt two"]'):
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    })


def _config(model):
    cfg = Config()
    cfg.api.chat_model = model
    return cfg


def test_reasoning_models_get_minimal_effort():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body.get("reasoning_effort"))
        return _ok(body)

    assert expand_query(_client(handler), _config("gpt-5-mini"), "q", 3) == ["q", "alt one", "alt two"]
    assert seen == ["minimal"]


def test_other_models_get_no_effort_hint():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append("reasoning_effort" in body)
        return _ok(body)

    expand_query(_client(handler), _config("llama-3.3-70b"), "q", 3)
    assert seen == [False]


def test_rejected_effort_hint_retries_without_it():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body.get("reasoning_effort"))
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'minimal'", "type": "invalid_request_error"}})
        return _ok(body)

    assert expand_query(_client(handler), _config("gpt-5.9"), "q", 3) == ["q", "alt one", "alt two"]
    assert seen == ["minimal", None]


def test_failure_falls_back_to_original_query_with_a_warning(caplog):
    def handler(req):
        return _ok(json.loads(req.content), content="not json")

    with caplog.at_level("WARNING", logger="wactx.search"):
        assert expand_query(_client(handler), _config("gpt-5-mini"), "q", 3) == ["q"]
    assert "Query expansion failed" in caplog.text
