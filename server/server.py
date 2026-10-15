from fastapi import FastAPI
from pydantic import BaseModel
from pathlib import Path
from fastapi.responses import FileResponse
from agent.config import load_config
from agent.graph import get_history, resume_chat, run_chat,list_sessions,new_session_id
app=FastAPI()
config=load_config()

class ChatRequest(BaseModel):
    message:str
    thread_id:str="default"

class ApproveRequest(BaseModel):
    approved: bool
    thread_id:str="default"

class ChatResponse(BaseModel):
    reply:str
    events:list[str]
    pending:dict|None=None
    stop_reason:str
    turns:int
    model_tier:str=""
    model_name:str=""
    
@app.post("/chat")
def chat(body:ChatRequest)->ChatResponse:
    result = run_chat(body.message,body.thread_id)
    return ChatResponse(
        reply=result["reply"],
        events=result["events"],
        pending=result.get("pending"),
        stop_reason=result["stop_reason"],
        turns=result["turns"],
        model_tier=result.get("model_tier") or "",
        model_name=result.get("model_name") or "",

    )

@app.post("/approve")
def approve(body: ApproveRequest) -> ChatResponse:
    result = resume_chat(body.approved,body.thread_id)
    return ChatResponse(
        reply=result["reply"],
        events=result["events"],
        pending=result.get("pending"),
        stop_reason=result["stop_reason"],
        turns=result["turns"],
        model_tier=result.get("model_tier") or "",
        model_name=result.get("model_name") or "",

    )

WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


@app.get("/")
def index():
    return FileResponse(WEB_INDEX)

@app.get("/history")
def history(thread_id:str="default"):
    return get_history(thread_id)

@app.get("/sessions")
def sessions():
    return list_sessions()

@app.post("/sessions")
def create_session():
    return {"id":new_session_id()}