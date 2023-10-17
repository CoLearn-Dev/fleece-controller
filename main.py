import asyncio
import logging
import multiprocessing
from typing import Annotated, AsyncGenerator
from datetime import datetime, timedelta
from fastapi import Depends, FastAPI, HTTPException, Header, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from jose import JWTError, jwt
import tiktoken

import jwt_secret
from database import SessionLocal
import models, schemas, crud
from scheduler import start_scheduler

enc = tiktoken.get_encoding("cl100k_base")
app = FastAPI()

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

@app.post("/register_worker/", response_model=schemas.WorkerToken)
def register_worker(worker: schemas.WorkerRegister, db: Session = Depends(get_db)):
    db_worker = crud.register_worker(db, worker.worker_url)
    return schemas.WorkerToken(access_token=create_access_token({"sub": db_worker.w_id}), token_type="bearer")

@app.post("/deregister_worker/")
def deregister_worker(w_id: Annotated[str, Depends(get_current_worker_id)], db: Session = Depends(get_db)):
    crud.deregister_worker(db, w_id)

def build_task_output_receiver(db_task: models.Task) -> AsyncGenerator[schemas.ChatCompletionResponseStreamChoice, None]:
    dummy_model_output = f"This is the dummy output for t_id={db_task.t_id}."
    tokenized = enc.encode(dummy_model_output)
    async def ret():
        for i in range(db_task.n):
            yield schemas.ChatCompletionResponseStreamChoice(
                index=i,
                delta=schemas.DeltaMessage(role="assistant"),
            )
            for t in tokenized:
                await asyncio.sleep(0.1)
                yield schemas.ChatCompletionResponseStreamChoice(
                    index=i,
                    delta=schemas.DeltaMessage(content=enc.decode([t])),
                    finish_reason=None,
                )
            yield schemas.ChatCompletionResponseStreamChoice(
                index=i,
                finish_reason="stop",
            )
    return ret()

def terminate_task(db_task: models.Task):
    raise NotImplementedError  # TODO

@app.post("/v1/chat/completions")
async def chat_completions(
    request: schemas.ChatCompletionRequest,
    raw_request: Request,
    Authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    # ref: https://platform.openai.com/docs/api-reference/chat
    db_task = crud.create_task(db, request)
    # Inform scheduler
    scheduler_q.put(db_task.t_id)
    response_id = db_task.t_id
    response_created = round(db_task.created_at.timestamp())
    response_model = request.model
    response_generator = build_task_output_receiver(db_task)
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
        indexed_delta_contents = [[] for _ in range(db_task.n)]
        indexed_finish_reason = [None for _ in range(db_task.n)]
        async for c in response_generator:
            if await raw_request.is_disconnected():
                terminate_task(db_task)
                raise HTTPException(status_code=400, detail="Client disconnected.")  # TODO: is this necessary?
            indexed_delta_contents[c.index].append(c.delta.content if c.delta.content is not None else "")
            indexed_finish_reason[c.index] = c.finish_reason
        prompt_tokens = sum(len(enc.encode(m.content)) for m in request.messages)
        completion_tokens = sum(
            len(delta_contents) - 2  # subtract 2 for the first role delta and last finish delta
            for delta_contents in indexed_delta_contents
        )
        return schemas.ChatCompletionResponse(
            id=response_id,
            created=response_created,
            model=response_model,
            choices=[
                schemas.ChatCompletionResponseChoice(
                    index=i,
                    message=schemas.ChatMessage(role="assistant", content=''.join(delta_contents)),
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

@app.post("/update_task/")
def update_task(task_update: schemas.TaskUpdate, w_id: Annotated[str, Depends(get_current_worker_id)], db: Session = Depends(get_db)):
    crud.process_task_update(db, w_id, task_update)

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    scheduler_p.start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    scheduler_p.join()