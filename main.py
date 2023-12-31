import asyncio
import logging
import multiprocessing
from typing import Annotated, AsyncGenerator, Dict, List
from datetime import datetime, timedelta
from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from jose import JWTError, jwt
import tiktoken

import jwt_secret
from database import SessionLocal
import models, schemas, crud
from scheduler import start_scheduler

from llama.tokenizer import Tokenizer  # LATER: move to a separate file
llama_enc = Tokenizer("./llama/tokenizer.model")
openai_enc = tiktoken.get_encoding("cl100k_base")

app = FastAPI()

origins = [
    "http://localhost",
    "http://localhost:7999",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Scheduler Process
scheduler_q = multiprocessing.Queue()
scheduler_p = multiprocessing.Process(target=start_scheduler, args=(scheduler_q,))

# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_access_token(data: dict):
    to_encode = data.copy()
    to_encode.update({"exp": datetime.utcnow() + timedelta(minutes=jwt_secret.ACCESS_TOKEN_EXPIRE_MINUTES)})
    return jwt.encode(to_encode, jwt_secret.SECRET_KEY, algorithm=jwt_secret.ALGORITHM)

def get_current_worker_id(worker_token: Annotated[str, Header()]):
    try:
        if worker_token is None:
            raise HTTPException(status_code=403, detail="Invalid authentication credentials. No valid Authorization header.")
        payload = jwt.decode(worker_token, jwt_secret.SECRET_KEY, algorithms=[jwt_secret.ALGORITHM])
        w_id: str = payload.get("sub")
        if w_id is None:
            raise HTTPException(status_code=403, detail="Invalid authentication credentials. Sub not found.")
    except JWTError:
        raise HTTPException(status_code=403, detail="Invalid authentication credentials. JWT Error.")
    return w_id

@app.post("/register_worker", response_model=schemas.WorkerToken)
def register_worker(worker: schemas.WorkerRegister, db: Session = Depends(get_db)):
    db_worker = crud.register_worker(db, worker.worker_url)
    return schemas.WorkerToken(access_token=create_access_token({"sub": db_worker.w_id}))

@app.post("/deregister_worker")
def deregister_worker(w_id: Annotated[str, Depends(get_current_worker_id)], db: Session = Depends(get_db)):
    crud.deregister_worker(db, w_id)

@app.get("/list_workers", response_model=List[schemas.Worker])
def list_workers(db: Session = Depends(get_db)):
    return [schemas.Worker(w_id=db_worker.w_id, worker_url=db_worker.worker_url, created_at=round(db_worker.created_at.timestamp())) for db_worker in crud.list_workers(db)]

receiver_queues: Dict[str, asyncio.Queue] = {}
fulfilled: Dict[str, List[bool]] = {}

def build_chat_session_receiver(c_id, model, n) -> AsyncGenerator[schemas.ChatCompletionResponseStreamChoice, None]:
    q = receiver_queues[c_id] = asyncio.Queue()
    fulfilled[c_id] = [False] * n
    assert model.startswith("llama-2-"), f"Model {model} is not supported."
    async def ret():
        for i in range(n):
            yield schemas.ChatCompletionResponseStreamChoice(
                index=i,
                delta=schemas.DeltaMessage(role="assistant"),
            )
        while True:
            (output_tokens, fulfilled_nw) = await q.get()  # TODO: check if there are ordering issues
            for i, t in enumerate(output_tokens):
                if fulfilled_nw[i]:
                    continue
                current_piece = llama_enc.sp_model.id_to_piece(t)
                yield schemas.ChatCompletionResponseStreamChoice(
                    index=i,
                    delta=schemas.DeltaMessage(content=f"[{t}]{current_piece}"),
                    finish_reason=None,
                )
                if t == llama_enc.eos_id:
                    yield schemas.ChatCompletionResponseStreamChoice(
                        index=i,
                        finish_reason="stop",
                    )
            q.task_done()
            if all(fulfilled_nw):
                break
    return ret()

def terminate_chat_session(db_chat_session: models.ChatSession):
    raise NotImplementedError  # TODO

@app.post("/v1/chat/completions")
async def chat_completions(
    request: schemas.ChatCompletionRequest,
    raw_request: Request,
    Authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    # ref: https://platform.openai.com/docs/api-reference/chat
    db_chat_session = crud.create_chat_session(db, request)
    # Inform scheduler
    scheduler_q.put(db_chat_session.c_id)
    response_id = db_chat_session.c_id
    response_created = round(db_chat_session.created_at.timestamp())
    response_model = request.model
    response_generator = build_chat_session_receiver(db_chat_session.c_id, request.model, db_chat_session.n)
    del db  # explicitly releasing the handle
    if request.stream:
        async def completion_stream_generator() -> AsyncGenerator[str, None]:
            async for c in response_generator:
                data_str = schemas.ChatCompletionStreamResponse(
                    id=response_id,
                    created=response_created,
                    model=response_model,
                    choices=[c],
                ).model_dump_json()
                yield f"data: {data_str}\n\n"
            yield f"data: [DONE]\n\n"
        return StreamingResponse(
            completion_stream_generator(),
            media_type="text/event-stream",
        )
    else:
        indexed_delta_contents = [[] for _ in range(db_chat_session.n)]
        indexed_finish_reason = [None for _ in range(db_chat_session.n)]
        async for c in response_generator:
            if await raw_request.is_disconnected():
                terminate_chat_session(db_chat_session)
                raise HTTPException(status_code=400, detail="Client disconnected.")  # TODO: is this necessary?
            indexed_delta_contents[c.index].append(c.delta.content if c.delta.content is not None else "")
            indexed_finish_reason[c.index] = c.finish_reason
        prompt_tokens = sum(len(openai_enc.encode(m.content)) for m in request.messages)
        # FIXME: align usage counting for different models
        completion_tokens = sum(
            len(delta_contents) - 2  # subtract 2 for the first role delta and last finish delta
            for delta_contents in indexed_delta_contents
        )
        def combine_tokens(delta_contents):
            assert response_model.startswith("llama-2-"), f"Model {response_model} is not supported."
            print("delta_content", delta_contents)
            return llama_enc.sp_model.decode([dc for dc in delta_contents if dc])
        return schemas.ChatCompletionResponse(
            id=response_id,
            created=response_created,
            model=response_model,
            choices=[
                schemas.ChatCompletionResponseChoice(
                    index=i,
                    message=schemas.ChatMessage(role="assistant", content=combine_tokens(delta_contents)),
                    finish_reason=finish_reason,
                )
                for i, (delta_contents, finish_reason) in enumerate(zip(indexed_delta_contents, indexed_finish_reason))
            ],
            usage=schemas.UsageInfo(
                prompt_tokens=prompt_tokens,  # note: the special tokens are not counted for now (e.g. B_INST, E_INST, B_SYS, E_SYS)
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

@app.post("/update_task")
def update_task(task_update: schemas.TaskUpdate, w_id: Annotated[str, Depends(get_current_worker_id)], db: Session = Depends(get_db)):
    db_task_progress = crud.create_task_progress(db, w_id, task_update)
    # TODO check output_status to see if any errs
    if task_update.output_tokens:
        output_tokens = task_update.output_tokens
        c_id = db_task_progress.from_t.from_c_id
        for i, t in enumerate(output_tokens):
            if fulfilled[c_id][i]:
                continue
            if t == llama_enc.eos_id:
                fulfilled[c_id][i] = True
        receiver_queues[c_id].put_nowait((output_tokens, fulfilled[c_id]))
        if all(fulfilled[c_id]):
            db_task_progress.from_t.status = "completed"
            db_task_progress.from_t.from_c.status = "completed"
            db.commit()
            receiver_queues.pop(c_id)
            fulfilled.pop(c_id)

if __name__ == "__main__":
    # logging.basicConfig(level=logging.DEBUG)
    import uvicorn
    logging.basicConfig(level=logging.CRITICAL)
    scheduler_p.start()
    uvicorn.run(app, host="0.0.0.0", port=8000, access_log=False)
    scheduler_p.join()
