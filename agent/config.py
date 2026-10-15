import os
from dotenv import load_dotenv
from pathlib import Path


load_dotenv()
def require_env(name:str)->str:
    value=os.getenv(name,"").strip() #strip（）去掉收尾空白字符
    if not value:
        raise ValueError(f"Environment variable {name} is not set")
    return value

def optional_env(name: str, fallback: str) -> str:
    value = os.getenv(name, "").strip()
    return value or fallback

def load_config()->dict:
    workspace=Path(require_env("WORKSPACE_DIR")).resolve()
    if not workspace.is_dir():
        raise SystemExit(f"Workspace_DIR不是文件夹{workspace}")
    model=require_env("OPENAI_MODEL")
    return {
        "api_key":require_env("OPENAI_API_KEY"),
        "base_url":require_env("OPENAI_BASE_URL"),
        "model":require_env("OPENAI_MODEL"),
        "models": {
            "lite": optional_env("OPENAI_MODEL_LITE", model),
            "default": optional_env("OPENAI_MODEL_DEFAULT", model),
            "craft": optional_env("OPENAI_MODEL_CRAFT", model),
        },
        "workspace":workspace,  
    }
