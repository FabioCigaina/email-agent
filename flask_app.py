from flask import Flask, render_template, jsonify, request
import threading
from collections import deque
from datetime import datetime
from uuid import uuid4
import traceback
from tenacity import retry
from tenacity.wait import wait_exponential
from tenacity.stop import stop_after_attempt

# ── Importa tutto dal tuo progetto ───────────────────────────────────────────
from googleapiclient.discovery import Resource
from langgraph.graph.state import CompiledStateGraph
from mail_parsing_logic import (
    get_service, VECTOR_PATH,
    get_agent, get_thread_message_ids, get_thread_emails,
    extract_header_from_payload, make_create_reply_tool,
    make_retrieve_context_tool, make_create_draft_tool,
)
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings
from dotenv import load_dotenv
import json, os, time

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]
CRED_FILE = "credentials.json"
TOKEN_FILE = "token.json"
LOGS_PATH = "email_logs.json"

SYSTEM_PROMPT_DIR = "system_prompts"

system_prompts = {}
for prompt in os.listdir(SYSTEM_PROMPT_DIR):
    bn = os.path.basename(prompt)
    with open(os.path.join(SYSTEM_PROMPT_DIR, prompt), "r") as f:
        try:
            system_prompts[os.path.splitext(bn)[0]] = f.read()
        except Exception as e:
            print(f"Couldn't load file {prompt}. {e}")

# ── Stato condiviso tra Flask e il thread del loop ───────────────────────────
activity_log: deque = deque(maxlen=200)   # log eventi visibili nella UI
loop_running = False
stop_event = threading.Event()
state_lock = threading.Lock()          # per leggere/scrivere loop_running in sicurezza

def add_log(level: str, message: str, thread_id: str = ""):
    """Adds a log to deque (callable from any thread)."""
    activity_log.append({
        "ts":        datetime.now().strftime("%H:%M:%S"),
        "level":     level,          # "info" | "ok" | "error" | "warn"
        "message":   message,
        "thread_id": thread_id,
    })

# Gmail + agents instantiation
add_log("info", "Gmail service initalization...")
svc = get_service(TOKEN_FILE, CRED_FILE, SCOPES)
user_info = svc.users().getProfile(userId="me").execute()
user_email = user_info["emailAddress"]
add_log("ok", f"Connected as {user_email}")

add_log("info", "Loading vector store")
embeddings    = OpenAIEmbeddings(model="text-embedding-3-large")
vector_store  = FAISS.load_local(VECTOR_PATH, embeddings, allow_dangerous_deserialization=True)
add_log("ok", "Vector store loaded")

add_log("info", "Initializing agents...")
agent_draft  = get_agent("gpt-5.2", [make_create_draft_tool(svc)],                                              user_email, system_prompts["draft"])
agent_reply  = get_agent("gpt-5.2", [make_create_reply_tool(svc)],                                             user_email, system_prompts["reply"])
agent_choice = get_agent("gpt-5.2", [make_create_draft_tool(svc), make_create_reply_tool(svc)],                user_email, system_prompts["choice"])
agent_rag    = get_agent("gpt-5.2", [make_create_draft_tool(svc), make_create_reply_tool(svc),
                                     make_retrieve_context_tool(vector_store)],                               user_email, system_prompts["rag"])

AGENTS = {
    "rag":    agent_rag,
    "draft":  agent_draft,
    "reply":  agent_reply,
    "choice": agent_choice,
}
add_log("ok", "All agents ready")


class EmailLogs:
    def __init__(self, path):
        self.path = path
        self.messages_set = set()

    def load(self):
        if not os.path.exists(self.path):
            open(self.path, "w", encoding="utf-8")
            self.write()
            return
        data = json.loads(open(self.path, "r").read())
        self.messages_set = set(data.get("message_ids", []))

    def write(self):
        json.dump({"message_ids": list(self.messages_set)}, open(self.path, "w"))

    def add_messages(self, message_ids):
        self.messages_set = self.messages_set.union(message_ids)


#api with retry
@retry(wait = wait_exponential(min = 1, max = 30), stop = stop_after_attempt(3))
def handle_thread_with_retry(agent : CompiledStateGraph, thread_id : str):
    return handle_thread(agent, thread_id)

# loop logic
def handle_thread(agent : CompiledStateGraph, thread_id : str):
    config = {"configurable": {"thread_id": str(uuid4())}}
    thread_json = get_thread_emails(svc, thread_id)
    resp = agent.invoke(
        {"messages": [("user", f"Thread JSON:\n{thread_json}")]},
        config=config,
    )
    last_msg = resp["messages"][-1]
    content  = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
    return content


def draft_loop(agent: CompiledStateGraph, period: int, starting_num: int,
               ignore_self_emails: bool = True):
    global loop_running
    email_logs = EmailLogs(LOGS_PATH)
    email_logs.load()
    add_log("info", f"Loop started, scanning emails every {period}s")

    while not stop_event.is_set():
        try:
            resp = svc.users().messages().list(userId="me", maxResults=starting_num).execute()
            messages = resp.get("messages", [])
            new_ids = [m["id"] for m in messages if m["id"] not in email_logs.messages_set]

            if new_ids:
                new_msgs = [svc.users().messages().get(userId="me", id=mid).execute() for mid in new_ids]

                if ignore_self_emails:
                    filtered = []
                    for m in new_msgs:
                        if extract_header_from_payload(m["payload"], "From") == user_email:
                            email_logs.add_messages([m["id"]])
                        else:
                            filtered.append(m)
                    new_msgs = filtered


                add_log("info", f"{len(new_msgs) if len(new_msgs) != 0 else 'No'} new messages found")
                new_thread_ids = set(m["threadId"] for m in new_msgs)
                for tid in new_thread_ids:
                    try:
                        add_log("info", f"Managing thread", thread_id=tid)
                        result = handle_thread_with_retry(agent, tid)
                        add_log("ok", f"Thread managed: {result}",
                                thread_id=tid)
                        email_logs.add_messages(get_thread_message_ids(svc, tid))
                    except Exception as e:
                        add_log("error", f"Thread error: {e}", thread_id=tid)

                email_logs.add_messages(new_ids)
                email_logs.write()
            else:
                add_log("info", "No new messages found")

        except Exception as e:
            add_log("error", f"Loop error: {traceback.format_exc(limit=2)}")

        stop_event.wait(period)   # sleep interrompibile dallo stop

    with state_lock:
        loop_running = False
    add_log("warn", "Loop stopped")


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html",
                           user_email=user_email,
                           agents=list(AGENTS.keys()))


@app.route("/api/status")
def api_status():
    with state_lock:
        running = loop_running
    return jsonify({
        "running":     running,
        "user_email":  user_email,
        "log_count":   len(activity_log),
    })


@app.route("/api/logs")
def api_logs():
    """Returns last N logs. ?since=index for updates."""
    since = int(request.args.get("since", 0))
    logs  = list(activity_log)
    return jsonify({"logs": logs[since:], "total": len(logs)})


@app.route("/api/start", methods=["POST"])
def api_start():
    global loop_running
    with state_lock:
        if loop_running:
            return jsonify({"ok": False, "msg": "Loop already executing"})
        loop_running = True

    data = request.get_json(silent=True) or {}
    agent_name = data.get("agent", "rag")
    period = int(data.get("period", 10))
    max_msgs = int(data.get("max_msgs", 5))
    agent = AGENTS.get(agent_name, agent_rag)

    stop_event.clear()
    t = threading.Thread(target=draft_loop,
                         args=(agent, period, max_msgs),
                         daemon=True)
    t.start()
    add_log("ok", f"Loop started with agent '{agent_name}', period {period}s")
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_event.set()
    add_log("warn", "Stop requested, waiting for current loop to end")
    return jsonify({"ok": True})



if __name__ == "__main__":
    app.run(debug=False, port=5000)
