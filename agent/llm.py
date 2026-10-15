from openai import OpenAI


# 接收的是文字
def complete(config:dict,user_message:str)->str:
    client=OpenAI(api_key=config["api_key"],base_url=config["base_url"])
    response=client.chat.completions.create(model=config["model"],
    messages=[{"role":"user","content":user_message}])

    text=response.choices[0].message.content
    if not text:
        raise SystemExit("模型没有返回文字")
    return text




def chat(config:dict,messages:list,tools:list):
    client=OpenAI(api_key=config["api_key"],base_url=config["base_url"])
    response=client.chat.completions.create(model=config["model"],
    messages=messages,
    tools=tools,
    max_tokens=8000,)
    return response.choices[0].message,response.choices[0].finish_reason


def complete_text(config: dict, user_message: str, max_tokens: int = 32) -> str:
    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
    response = client.chat.completions.create(
        model=config["model"],
        messages=[{"role": "user", "content": user_message}],
        max_tokens=max_tokens,
    )
    return (response.choices[0].message.content or "").strip()


def complete_json(config: dict, user_message: str, max_tokens: int = 800) -> str:
    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
    response = client.chat.completions.create(
        model=config["model"],
        messages=[{"role": "user", "content": user_message}],
        max_tokens=max_tokens,
        temperature=0,
    )
    return (response.choices[0].message.content or "").strip()