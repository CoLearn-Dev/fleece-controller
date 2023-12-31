import multiprocessing
import uuid
import time
import requests
import json
from logging import getLogger


from database import SessionLocal
import models, schemas
from llama.tokenizer import Tokenizer  # LATER: move to a separate file

logger = getLogger()
db = SessionLocal()
enc = Tokenizer("./llama/tokenizer.model")
# Ref: https://github.com/facebookresearch/llama/blob/1c95a19e8c7b0363c7808ff4f6f1aec3545e4ec6/llama/generation.py#L44
B_INST, E_INST = "[INST]", "[/INST]"
B_SYS, E_SYS = "<<SYS>>\n", "\n<</SYS>>\n\n"
SPECIAL_TAGS = [B_INST, E_INST, "<<SYS>>", "<</SYS>>"]
UNSAFE_ERROR = "Error: special tags are not allowed as part of the prompt."


def send_request_to_worker(db_task: models.Task, db_worker: models.Worker):
    plan = json.loads(db_task.plan)
    if db_task.from_c.model.startswith("llama-2-"):
        dialog = schemas.ChatMessageList.model_validate_json(db_task.from_c.messages)
        # Ref: https://github.com/facebookresearch/llama/blob/1c95a19e8c7b0363c7808ff4f6f1aec3545e4ec6/llama/generation.py#L318
        assert not any([tag in msg.content for tag in SPECIAL_TAGS for msg in dialog]), UNSAFE_ERROR
        if dialog[0].role == "system":
            dialog = [schemas.ChatMessage(role=dialog[1].role, 
                content=B_SYS
                + dialog[0].content
                + E_SYS
                + dialog[1].content)] + dialog[2:]
        assert all([msg.role == "user" for msg in dialog[::2]]) and all(
            [msg.role == "assistant" for msg in dialog[1::2]]
            ), (
                "model only supports 'system', 'user' and 'assistant' roles, "
                "starting with 'system', then 'user' and alternating (u/a/u/a/u...)"
            )
        prompt_tokens = sum([
            enc.encode(
                f"{B_INST} {(prompt.content).strip()} {E_INST} {(answer.content).strip()} ",
                bos=True,
                eos=True,
            )
            for prompt, answer in zip(
                dialog[::2],
                dialog[1::2],
            )
        ], [])
        assert dialog[-1].role == "user", f"Last message must be from user, got {dialog[-1]['role']}"
        prompt_tokens += enc.encode(
            f"{B_INST} {(dialog[-1].content).strip()} {E_INST}",
            bos=True,
            eos=False,
        )
    else:
        # TODO: support other tokenizers
        raise NotImplementedError
    request_json = {
        "task_id": db_task.t_id,
        "is_new_task": True,
        "plan": plan,
        "step": 0,
        "round": 0,
        "payload": [prompt_tokens]
    }
    logger.info(f"--> Request to {db_worker.worker_url}, JSON: " + json.dumps(request_json))
    request = requests.post(
        f"{db_worker.worker_url}/forward",  # FIXME: dangerous operation to visit a URL from database
        json=request_json,
        timeout=10,  # TODO: determine timeout based on network conditions
    )
    assert request.status_code == 200, f"Request to worker failed with status code {request.status_code}"


def schedule(db_chat_session: models.ChatSession):
    # TODO: implement scheduling
    # Note: BELOW IS THE DUMMY IMPLEMENTATION
    def randomly_choose_worker():
        import random
        all_workers = db.query(models.Worker).all()
        if len(all_workers) == 0:
            raise Exception("No worker exist.")
        return random.choice(all_workers)
    db_worker = randomly_choose_worker()
    # TODO: support other models
    plan = [(db_worker.worker_url, [
        "llama-2-7b-chat-slice/tok_embeddings",
        *[f"llama-2-7b-chat-slice/layers.{i}" for i in range(32)],
        "llama-2-7b-chat-slice/norm",
        "llama-2-7b-chat-slice/output",
    ])]
    db_chat_session.status = "scheduled"
    db_task = models.Task(
        t_id=uuid.uuid4().hex, 
        status="created", 
        from_c_id=db_chat_session.c_id,
        plan=json.dumps(plan),
        plan_step_num=len(plan),
        plan_current_step=-1,
        plan_current_round=0,
    )
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    send_request_to_worker(db_task, db_worker)

def start_scheduler(q):
    logger.info("Scheduler started.")
    while True:
        c_id = q.get()
        db_chat_session = db.query(models.ChatSession).filter(models.ChatSession.c_id == c_id).first()
        if db_chat_session is None:
            break
        try:
            schedule(db_chat_session)
        except Exception as e:
            # print stack trace
            import traceback
            traceback.print_exc()
            logger.error(f"Error in scheduling task {db_chat_session.c_id}: {e}")
            db_chat_session.status = "error: " + str(e)
            db.commit()

def generate_dummy_db_chat_session():
    return models.ChatSession(
        c_id=uuid.uuid4().hex, 
        status="pending", 
        stream=True, 
        model="llama-2-7b-chat", 
        messages=schemas.ChatMessageList([
            schemas.ChatMessage(role="user", content="This is a dummy task generated by `scheduler.py`."),
        ]).model_dump_json(), 
        n=1,
    )

if __name__ == "__main__":
    q = multiprocessing.Queue()
    p = multiprocessing.Process(target=start_scheduler, args=(q,))
    p.start()
    # create dummy tasks
    db_chat_session = generate_dummy_db_chat_session()
    db.add(db_chat_session)
    db.commit()
    db.refresh(db_chat_session)
    q.put(db_chat_session.c_id)
    time.sleep(1)
    print("Terminate in 3 seconds", end="", flush=True)
    for i in range(3):
        time.sleep(1)
        print(".", end="", flush=True)
    print()
    q.put(None)
    p.join()
