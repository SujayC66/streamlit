import os
import time
import urllib.request
from queue import Empty, Queue
from threading import Thread

import socketio as socketio_client
import streamlit as st
from flask import Flask, request
from flask_socketio import SocketIO, disconnect, join_room
from langchain_groq import ChatGroq
from werkzeug.security import check_password_hash, generate_password_hash

# -----------------------------
# Config
# -----------------------------
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "5050"))
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"


def get_secret(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.getenv(name, default)


GROQ_API_KEY = "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"
GROQ_MODEL = get_secret("GROQ_MODEL", "llama-3.3-70b-versatile")

# Demo login users
USERS_DB = {
    "user1@example.com": generate_password_hash("1234"),
    "user2@example.com": generate_password_hash("5678"),
    "user3@example.com": generate_password_hash("91011"),
    "user4@example.com": generate_password_hash("1213"),
}

# -----------------------------
# Flask + Socket.IO backend
# -----------------------------
flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = get_secret("FLASK_SECRET_KEY", "dev-secret-change-me")

backend_socket = SocketIO(
    flask_app,
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)

llm = ChatGroq(api_key=GROQ_API_KEY, model=GROQ_MODEL, temperature=0) if GROQ_API_KEY else None
user_map = {}  # sid -> email


@flask_app.get("/health")
def health():
    return {"status": "ok"}, 200


@backend_socket.on("connect")
def on_connect(auth):
    email = (auth or {}).get("email")
    if not email:
        return False
    sid = request.sid
    user_map[sid] = email
    join_room(sid)


@backend_socket.on("disconnect")
def on_disconnect():
    user_map.pop(request.sid, None)


@backend_socket.on("chat")
def on_chat(data):
    sid = request.sid
    if sid not in user_map:
        disconnect()
        return

    message = (data or {}).get("message", "").strip()
    if not message:
        backend_socket.emit("done", {"response": "Message cannot be empty.", "status": "error"}, room=sid)
        return

    backend_socket.start_background_task(process_chat, sid, message)


def process_chat(sid: str, message: str):
    if llm is None:
        backend_socket.emit(
            "done",
            {"response": "Missing GROQ_API_KEY. Set it in env or Streamlit secrets.", "status": "error"},
            room=sid,
        )
        return

    full = ""
    status = "done"
    try:
        for chunk in llm.stream(message):
            token = getattr(chunk, "content", "")
            if token:
                full += token
                backend_socket.emit("stream", {"token": token}, room=sid)
                backend_socket.sleep(0)
    except Exception as exc:
        full = str(exc)
        status = "error"

    backend_socket.emit("done", {"response": full, "status": status}, room=sid)


def run_backend():
    backend_socket.run(
        flask_app,
        host=BACKEND_HOST,
        port=BACKEND_PORT,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )


def backend_up() -> bool:
    try:
        with urllib.request.urlopen(f"{BACKEND_URL}/health", timeout=0.3) as r:
            return r.status == 200
    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def start_backend_once() -> bool:
    if backend_up():
        return True
    t = Thread(target=run_backend, daemon=True)
    t.start()
    for _ in range(80):
        if backend_up():
            return True
        time.sleep(0.1)
    return False


# -----------------------------
# Streamlit frontend
# -----------------------------
st.set_page_config(page_title="Login + Chatbot", page_icon=":speech_balloon:")
if not start_backend_once():
    st.error(f"Backend failed to start at {BACKEND_URL}")
    st.stop()


def init_state():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if "user_email" not in st.session_state:
        st.session_state.user_email = ""
    if "sio" not in st.session_state:
        st.session_state.sio = socketio_client.Client(reconnection=True)
    if "handlers_registered" not in st.session_state:
        st.session_state.handlers_registered = False
    if "connected" not in st.session_state:
        st.session_state.connected = False
    if "event_queue" not in st.session_state:
        st.session_state.event_queue = Queue()
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "current_response" not in st.session_state:
        st.session_state.current_response = ""
    if "done" not in st.session_state:
        st.session_state.done = False
    if "status" not in st.session_state:
        st.session_state.status = ""


def register_handlers():
    if st.session_state.handlers_registered:
        return

    sio = st.session_state.sio
    q = st.session_state.event_queue

    @sio.on("connect")
    def _on_connect():
        q.put(("connected", None))

    @sio.on("disconnect")
    def _on_disconnect():
        q.put(("disconnected", None))

    @sio.on("stream")
    def _on_stream(data):
        q.put(("stream", data.get("token", "")))

    @sio.on("done")
    def _on_done(data):
        q.put(("done", data))

    st.session_state.handlers_registered = True


def drain_events():
    q = st.session_state.event_queue
    while True:
        try:
            event, payload = q.get_nowait()
        except Empty:
            break

        if event == "connected":
            st.session_state.connected = True
        elif event == "disconnected":
            st.session_state.connected = False
        elif event == "stream":
            st.session_state.current_response += payload
        elif event == "done":
            st.session_state.current_response = payload.get("response", "")
            st.session_state.status = payload.get("status", "")
            st.session_state.done = True


def connect_socket(email: str):
    sio = st.session_state.sio
    if not sio.connected:
        sio.connect(BACKEND_URL, auth={"email": email}, wait_timeout=10)
    st.session_state.connected = True


def reset_socket_client():
    try:
        if st.session_state.sio.connected:
            st.session_state.sio.disconnect()
    except Exception:
        pass
    st.session_state.sio = socketio_client.Client(reconnection=True)
    st.session_state.handlers_registered = False
    st.session_state.connected = False
    st.session_state.event_queue = Queue()


def logout():
    reset_socket_client()
    st.session_state.authenticated = False
    st.session_state.user_email = ""
    st.session_state.messages = []
    st.session_state.current_response = ""
    st.session_state.done = False
    st.session_state.status = ""


init_state()
register_handlers()
drain_events()

# -----------------------------
# Page 1: Login
# -----------------------------
if not st.session_state.authenticated:
    st.title("Login")
    st.caption("Demo users: user1@example.com/1234, user2@example.com/5678, user3@example.com/9101, user4@example.com/1123")

    with st.form("login_form"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)

    if submitted:
        if email in USERS_DB and check_password_hash(USERS_DB[email], password):
            st.session_state.authenticated = True
            st.session_state.user_email = email
            try:
                connect_socket(email)
            except Exception as exc:
                st.session_state.authenticated = False
                st.error(f"Socket connect failed: {exc}")
                st.stop()
            st.rerun()
        else:
            st.error("Invalid email or password")

    st.stop()

# -----------------------------
# Page 2: Chatbot
# -----------------------------
st.title("Chatbot")

with st.sidebar:
    st.write(f"Logged in as: `{st.session_state.user_email}`")
    st.write(f"Backend: `{BACKEND_URL}`")
    if st.button("Logout", use_container_width=True):
        logout()
        st.rerun()

if not GROQ_API_KEY:
    st.warning("GROQ_API_KEY is missing. Add it in env or Streamlit secrets.")

if not st.session_state.connected:
    try:
        connect_socket(st.session_state.user_email)
    except Exception as exc:
        st.error(f"Cannot connect to backend: {exc}")
        st.stop()

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

prompt = st.chat_input("Type your message")
if prompt:
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    st.session_state.current_response = ""
    st.session_state.done = False
    st.session_state.status = ""
    st.session_state.sio.emit("chat", {"message": prompt})

    with st.chat_message("assistant"):
        ph = st.empty()
        timeout_at = time.time() + 180
        while not st.session_state.done and time.time() < timeout_at:
            drain_events()
            ph.markdown(st.session_state.current_response)
            time.sleep(0.05)
        drain_events()
        ph.markdown(st.session_state.current_response)

    st.session_state.messages.append({"role": "assistant", "content": st.session_state.current_response})
    if st.session_state.status and st.session_state.status != "done":
        st.caption(f"Status: {st.session_state.status}")
