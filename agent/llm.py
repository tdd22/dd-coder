import os
from contextlib import ExitStack, contextmanager


def _tracing_enabled() -> bool:
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
        and os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    )


def _client(config: dict):
    if _tracing_enabled():
        try:
            from langfuse.openai import OpenAI
            return OpenAI(api_key=config["api_key"], base_url=config["base_url"]), True
        except ModuleNotFoundError as exc:
            print(f"langfuse skipped: {exc}")
    from openai import OpenAI
    return OpenAI(api_key=config["api_key"], base_url=config["base_url"]), False


def _create(config: dict, name: str, **kwargs):
    client, tracing = _client(config)
    if tracing:
        kwargs["name"] = name
    return client.chat.completions.create(**kwargs)


@contextmanager
def traced_turn(thread_id: str, name: str):
    if not _tracing_enabled():
        yield
        return
    stack = ExitStack()
    langfuse = None
    ready = False
    try:
        from langfuse import get_client, propagate_attributes

        langfuse = get_client()
        stack.enter_context(
            langfuse.start_as_current_observation(as_type="span", name=name)
        )
        stack.enter_context(
            propagate_attributes(session_id=thread_id, tags=["dd-coder"])
        )
        ready = True
    except Exception as exc:
        stack.close()
        print(f"langfuse skipped: {exc}")
    if not ready:
        yield
        return
    try:
        with stack:
            yield
    finally:
        try:
            langfuse.flush()
        except Exception as exc:
            print(f"langfuse flush skipped: {exc}")


def complete(config: dict, user_message: str) -> str:
    response = _create(
        config,
        "complete",
        model=config["model"],
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.choices[0].message.content
    if not text:
        raise SystemExit("模型没有返回文字")
    return text


def chat(config: dict, messages: list, tools: list):
    response = _create(
        config,
        "agent",
        model=config["model"],
        messages=messages,
        tools=tools,
        max_tokens=8000,
    )
    return response.choices[0].message, response.choices[0].finish_reason


def complete_text(config: dict, user_message: str, max_tokens: int = 32) -> str:
    response = _create(
        config,
        "complete_text",
        model=config["model"],
        messages=[{"role": "user", "content": user_message}],
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


def complete_json(config: dict, user_message: str, max_tokens: int = 800) -> str:
    response = _create(
        config,
        "complete_json",
        model=config["model"],
        messages=[{"role": "user", "content": user_message}],
        max_tokens=max_tokens,
        temperature=0,
    )
    return (response.choices[0].message.content or "").strip()
