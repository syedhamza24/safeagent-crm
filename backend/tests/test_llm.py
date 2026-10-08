"""Tests use a fake LLM client, so no API key or internet is needed."""
from types import SimpleNamespace

from app.llm import build_tools, propose


class FakeClient:
    def __init__(self, message=None, raise_error=False):
        self._message = message
        self._raise = raise_error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.last_kwargs = None

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._raise:
            raise RuntimeError("boom")
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)])


def tool_message(name, arguments):
    call = SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))
    return SimpleNamespace(content=None, tool_calls=[call])


def test_build_tools_has_all_four_tools():
    names = {t["function"]["name"] for t in build_tools()}
    assert names == {"query_order_status", "update_customer_record", "process_refund", "delete_record"}


def test_parses_tool_call():
    client = FakeClient(tool_message("process_refund", '{"order_id":"ORD-1002","amount_cents":2000,"reason":"damaged"}'))
    p = propose([{"role": "user", "content": "refund $20"}], "CUS-001", client=client)
    assert p.tool == "process_refund"
    assert p.args["amount_cents"] == 2000
    assert p.error is None


def test_plain_text_reply():
    client = FakeClient(SimpleNamespace(content="Which order do you mean?", tool_calls=None))
    p = propose([{"role": "user", "content": "refund me"}], "CUS-001", client=client)
    assert p.tool is None
    assert p.reply == "Which order do you mean?"


def test_malformed_arguments_are_handled():
    client = FakeClient(tool_message("process_refund", "{not json"))
    p = propose([{"role": "user", "content": "x"}], "CUS-001", client=client)
    assert p.error is not None and p.tool is None


def test_api_failure_does_not_crash():
    p = propose([{"role": "user", "content": "x"}], "CUS-001", client=FakeClient(raise_error=True))
    assert p.error is not None


def test_customer_id_is_in_system_prompt_and_temperature_is_zero():
    client = FakeClient(SimpleNamespace(content="hi", tool_calls=None))
    propose([{"role": "user", "content": "hi"}], "CUS-002", client=client)
    assert "CUS-002" in client.last_kwargs["messages"][0]["content"]
    assert client.last_kwargs["temperature"] == 0
