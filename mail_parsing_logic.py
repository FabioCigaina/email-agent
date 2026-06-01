import base64
from email.mime.text import MIMEText
import os
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dataclasses import dataclass, asdict
from dotenv import load_dotenv
from langchain.agents import create_agent  # NUOVA IMPORTAZIONE
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from uuid import uuid4
from langchain_openai import OpenAIEmbeddings
import faiss
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from langchain_community.document_loaders.csv_loader import CSVLoader
import requests

SCOPES = ["https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.compose",
        ]

CRED_FILE = "credentials.json"
TOKEN_FILE = "Ctoken.json"

VECTOR_PATH = "faiss_index"


load_dotenv()


@dataclass
class Email:
    subject : str
    body : str
    sender : str

    def to_dict(self):
        return asdict(self)
    
    def __str__(self):
        return f"Subject: {self.subject}\nSender: {self.sender}\nBody: {self.body}"

def get_service(token_file, cred_file, scopes):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(cred_file, scopes)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)

def list_messages(service, q = None, max_results = 10):
    resp = service.users().messages().list(userId = "me", q = q, maxResults = max_results).execute()
    return resp.get("messages", [])

def list_threads(service):
    resp = service.users().threads().list(userId = "me").execute()
    return resp.get("threads", [])

def get_thread_message_ids(service, thread_id):
    try:
        tdata = service.users().threads().get(userId = "me", id = thread_id).execute()
        return [m["id"] for m in tdata["messages"]]
    except Exception as e:
        print(f"Failure in getting thread message ids. {e}")


def get_message(service, msg_id, format = "full"):
    msg = service.users().messages().get(userId = "me", id = msg_id, format = format).execute()
    return msg

def extract_text_from_payload(payload):
    if "body" in payload and payload["body"] and payload["body"].get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data + "==").decode(errors = "replace")
    
    if "parts" in payload:
        for part in payload["parts"]:
            mime = part.get("mimeType", "")
            if mime == "text/plain":
                data = part["body"].get("data")
                if data:
                    return base64.urlsafe_b64decode(data + "==").decode(errors = "replace")
                
        for part in payload["parts"]:
            if part.get("parts"):
                text = extract_text_from_payload(part)
                if text:
                    return text
    return ""

def extract_header_from_payload(payload, header):
    headers = payload["headers"]
    for h in headers:
        if h["name"] == header:
            return h["value"]

def send_simple_email(service, to, subject, body):
    msg = MIMEText(body)
    msg["to"] = to
    msg["from"] = "me"
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()

def get_thread_emails(svc, thread_id):
    thread = svc.users().threads().get(userId = "me", id = thread_id).execute()
    thread_msgs = thread["messages"]
    emails = []
    for msg in thread_msgs:
        msg_id = msg["id"]
        full = get_message(svc, msg_id)
        body_text = extract_text_from_payload(full["payload"])
        headers = {h["name"]: h["value"] for h in full["payload"].get("headers", [])}
        from_h = headers.get("From", "Unknown")
        subj = headers.get("Subject", "(no subject)")
        emails.append(Email(subj, body_text, from_h))

    return {
        "thread_id" : thread_id,
        "emails" : [email.to_dict() for email in emails]
    }

def make_create_reply_tool(service):
    @tool("create_reply", description = "Create an email draft, specifying: to, subject, body.")
    def create_reply(to, subject, body):
        #service = get_service(TOKEN_FILE, CRED_FILE, SCOPES)
        #print(to, subject, body) for debugging
        msg = MIMEText(body)
        msg["to"] = to
        msg["from"] = "me"
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        message = service.users().messages().send(userId = "me", body = {"raw" : raw}).execute()
    return create_reply

def make_create_draft_tool(service):
    @tool("create_draft", description = "Create an email draft, specifying: to, subject, body.")
    def create_draft(to, subject, body):
        #service = get_service(TOKEN_FILE, CRED_FILE, SCOPES)
        #print(to, subject, body) for debugging
        msg = MIMEText(body)
        msg["to"] = to
        msg["from"] = "me"
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        create_message = {"message" : {"raw" : raw}}
        draft = service.users().drafts().create(userId = "me", body = create_message).execute()
        #return draft
    return create_draft

def make_retrieve_context_tool(vector_store : FAISS):
    @tool(response_format="content_and_artifact", description = "Retrieve information to help answer a query.")
    def retrieve_context(query: str):
        retrieved_docs = vector_store.similarity_search(query, k=2)
        serialized = "\n\n".join(
            (f"Source: {doc.metadata}\nContent: {doc.page_content}")
            for doc in retrieved_docs
        )
        return serialized, retrieved_docs
    return retrieve_context

def elaborate_drafts(agent, last_n = 10):
    service = get_service()
    thread_ids = (t.get("id", None) for t in list_threads(service)[:last_n])

    for thread_id in thread_ids:
        print(f"Thread id: {thread_id}")
        print("-"*50)
        lang_thread_id = str(uuid4())
        config = {"configurable": {"thread_id": lang_thread_id}}
        resp = agent.invoke(
            {
            "messages" : [
                    ("user", f"Thread id: {thread_id}. ")
                ]
            },
            config = config
        )

        assistant_message = resp["messages"][-1]

        #gestisco in modo diverso se è messaggio o chiamata tool
        if hasattr(assistant_message, "content"):
            print(f"\nAI: {assistant_message.content}")
        else:
            print(f"\nAI: {str(assistant_message)}")


def chat_loop(agent):
    lang_thread_id = str(uuid4())
    print(f"Session {lang_thread_id}")
    config = {"configurable": {"thread_id": lang_thread_id}}

    while True:
        user_input = input("\nTu: ")
        if user_input.lower() in ["q", "exit"]:
            break

        if not user_input:
            continue

        resp = agent.invoke(
            {
            "messages" : [
                    ("user", user_input)
                ]
            },
            config = config
        )

        assistant_message = resp["messages"][-1]

        #gestisco in modo diverso se è messaggio o chiamata tool
        if hasattr(assistant_message, "content"):
            print(f"\nAI: {assistant_message.content}")
        else:
            print(f"\nAI: {str(assistant_message)}")


def get_agent(model, tools, user_email, prompt):
    checkpointer = MemorySaver()
    agent = create_agent(
        model = model,
        tools = tools,
        system_prompt = f"You are a helpful email assistant, working for {user_email}. {prompt}",
        checkpointer = checkpointer
    )
    return agent

def stateful_service_tool(tool_func, service):
    def wrapper(*args, **kwargs):
        return tool_func(service, *args, **kwargs)
    return wrapper
