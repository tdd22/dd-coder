from enum import Enum
from dataclasses import dataclass

class ModelTier(str, Enum):
    LITE = "lite"
    DEFAULT = "default"
    CRAFT = "craft"


TASK_TIER = {
    "agent": ModelTier.CRAFT,
    "title": ModelTier.LITE,
    "pending_extract": ModelTier.LITE,
    "pending_verify": ModelTier.LITE,
}

@dataclass(frozen=True)
class Route:
    tier:ModelTier
    tools:tuple[str,...]
    model:str



# 拿task去查Task_tier,把查到的层级封进Route里面
# 带上config去找对应的模型
def route_task(config:dict,task: str) -> Route:
    try:
        tier = TASK_TIER[task]
    except KeyError as exc:
        raise ValueError(f"unknown task: {task}") from exc
    model=config["models"][tier.value]
    if task == "agent":
        return Route(tier=tier, model=model,tools=("tools",))
    return Route(tier=tier, model=model,tools=())