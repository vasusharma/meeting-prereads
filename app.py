# ------------------------------------------------------
#  Calendar Preread Assistant  —  Streamlit application
# ------------------------------------------------------
"""
Creates daily prereads for your meetings:
• pulls Google Calendar events
• finds latest “Granola” Gmail note for each title
• sends a single morning briefing via Gmail
"""

from __future__ import annotations

import base64
import datetime as dt
import email
import os
import re
from pathlib import Path
from typing import Dict, List
import json 

import requests
import streamlit as st
from apscheduler.schedulers.background import BackgroundScheduler
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from openai import OpenAI

# -----------------------------
#  CONFIG & SECRETS
# -----------------------------
def secret(key: str, default: str = "") -> str:
    """Fetch from Streamlit secrets or env var."""
    return st.secrets.get(key, os.getenv(key, default))

GOOGLE_CLIENT_ID     = secret("google_client_id")
GOOGLE_CLIENT_SECRET = secret("google_client_secret")
OPENAI_API_KEY       = secret("openai_api_key")
REDIRECT_URI         = secret("google_redirect_uri", "http://localhost:8501/auth_callback")
DAILY_PREREAD_CRON   = secret("daily_preread_cron", "0 6 * * *")   # 06:00 (minute hour)

OPENAI_MODEL = secret("openai_model", "gpt-4o-mini")

SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

TOKEN_FILE = Path("token.json")
client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------
#  STREAMLIT PAGE CONFIG
# -----------------------------
st.set_page_config(
    page_title="Calendar Preread Assistant",
    page_icon="📆",
    layout="wide",
    menu_items={
        "Report a bug": "mailto:support@example.com",
        "About": "Creates daily prereads from your Calendar, Gmail & Granola notes.",
    },
)

# -----------------------------
#  UTILS – CREDENTIAL HANDLING
# -----------------------------
def save_credentials(creds: Credentials) -> None:
    TOKEN_FILE.write_text(creds.to_json())
    st.session_state["creds"] = creds.to_json()

def load_credentials() -> Credentials | None:
    if "creds" in st.session_state:
        return Credentials.from_authorized_user_info(
            json.loads(st.session_state["creds"]), SCOPES
        )
    if TOKEN_FILE.exists():
        return Credentials.from_authorized_user_info(
            json.loads(TOKEN_FILE.read_text()), SCOPES
        )
    return None


def build_flow() -> Flow:
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uris": [REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )

def show_login_button() -> None:
    flow = build_flow()
    auth_url, _ = flow.authorization_url(
        prompt="consent", access_type="offline", include_granted_scopes="true"
    )
    st.markdown(f"[**🔐 Sign in with Google**]({auth_url})", unsafe_allow_html=True)

def handle_auth_callback() -> None:
    """Capture ?code=… after Google redirects back."""
    code = st.experimental_get_query_params().get("code")
    if code:
        flow = build_flow()
        flow.fetch_token(code=code[0])
        save_credentials(flow.credentials)
        # clear query params
        st.experimental_set_query_params()
        st.experimental_rerun()

# -----------------------------
#  GOOGLE  HELPERS
# -----------------------------
def fetch_todays_events(creds: Credentials) -> List[Dict]:
    svc = build("calendar", "v3", credentials=creds)
    tz = dt.datetime.now().astimezone().tzinfo
    start = dt.datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + dt.timedelta(days=1, microseconds=-1)
    events = (
        svc.events()
        .list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )
    return events

def extract_emails(event: Dict) -> List[str]:
    emails = {a.get("email") for a in event.get("attendees", []) if a.get("email")}
    if event.get("creator", {}).get("email"):
        emails.add(event["creator"]["email"])
    return list(emails)

# -----------------------------
#  GMAIL  HELPERS
# -----------------------------
def gmail_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)

def latest_granola_note(creds: Credentials, title: str, emails: List[str]) -> str:
    """Search Gmail for the most recent Granola mail matching the meeting title."""
    gsvc = gmail_service(creds)
    query = f"\"{title}\" Granola " + " ".join([f"(from:{e} OR to:{e})" for e in emails])
    resp = gsvc.users().messages().list(userId="me", q=query, maxResults=1).execute()
    msgs = resp.get("messages", [])
    if not msgs:
        return "No prior Granola notes found."

    msg = gsvc.users().messages().get(userId="me", id=msgs[0]["id"], format="full").execute()
    payload = msg["payload"]
    data = None
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"].startswith("text/plain"):
                data = part["body"]["data"]
                break
    if not data:  # fall back to full body
        data = payload.get("body", {}).get("data", "")
    try:
        text = base64.urlsafe_b64decode(data).decode()
    except Exception:
        text = "[Could not decode note]"
    return f"Previous Granola Note:\n{text.strip()[:2000]}"

def gmail_profile_email(creds: Credentials) -> str:
    return gmail_service(creds).users().getProfile(userId="me").execute()["emailAddress"]

def send_email(creds: Credentials, subject: str, body: str, to_addr: str) -> None:
    gsvc = gmail_service(creds)
    msg = email.message.EmailMessage()
    msg["To"] = to_addr
    msg["From"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gsvc.users().messages().send(userId="me", body={"raw": raw}).execute()

# -----------------------------
#  OPENAI SUMMARISATION
# -----------------------------
def summarise(event: Dict, note: str) -> str:
    attendees = ", ".join(extract_emails(event))
    prompt = f"""You are an executive assistant. Create a concise preread.

MEETING: {event.get("summary")}
WHEN: {event['start'].get('dateTime', event['start'].get('date'))}
ATTENDEES: {attendees}

{note}

Headings: **Objective**, **Key Context**, **Questions / Decisions**, **Logistics**."""
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

# -----------------------------
#  DAILY JOB
# -----------------------------
def daily_preread_job() -> None:
    creds = load_credentials()
    if not creds or not creds.valid:
        return
    events = fetch_todays_events(creds)
    summaries = []
    for ev in events:
        note = latest_granola_note(creds, ev.get("summary", ""), extract_emails(ev))
        summaries.append(summarise(ev, note))

    sender = gmail_profile_email(creds)
    if summaries:
        body = "\n\n---\n\n".join(summaries)
        subject = "Daily Meeting Prereads"
    else:
        body = "You're all clear today! 🎉\n\nNo meetings were found on your calendar."
        subject = "No Meetings Today 😊"

    try:
        send_email(creds, subject, body, sender)
        st.sidebar.success(f"Sent email: {subject}")
    except HttpError as e:
        st.sidebar.error(f"Gmail API error — {e}")

# -----------------------------
#  SCHEDULER (fire once)
# -----------------------------
if "sched" not in st.session_state:
    cron_min, cron_hour, *_ = DAILY_PREREAD_CRON.split()
    scheduler = BackgroundScheduler()
    scheduler.add_job(daily_preread_job, "cron", hour=cron_hour, minute=cron_min)
    scheduler.start()
    st.session_state["sched"] = True

# -----------------------------
#  AUTH FLOW
# -----------------------------
handle_auth_callback()
creds = load_credentials()

# -----------------------------
#  SIDEBAR
# -----------------------------
with st.sidebar:
    st.title("Preread Assistant")
    st.caption("AI‑powered briefing generator")

    if creds and creds.valid:
        st.success(f"Signed in as **{gmail_profile_email(creds)}**")
        if st.button("↻ Run preread now", use_container_width=True):
            daily_preread_job()
    else:
        show_login_button()

    st.markdown("---")
    st.subheader("Schedule")
    current_time = dt.time(hour=int(DAILY_PREREAD_CRON.split()[1]),
                           minute=int(DAILY_PREREAD_CRON.split()[0]))
    st.time_input("Daily e‑mail (server TZ)", current_time, disabled=True)
    st.caption("Change via `daily_preread_cron` secret.")
    st.markdown("---")
    st.caption("© 2025 Your Company")

# -----------------------------
#  MAIN TABS
# -----------------------------
tab_prev, tab_events, tab_about = st.tabs(
    ["📨  Inbox Preview", "📅 Today's Events", "ℹ️ About"]
)

with tab_prev:
    st.header("Upcoming briefing")
    if not creds or not creds.valid:
        st.info("Please sign in to preview your briefing.")
    else:
        evs = fetch_todays_events(creds)
        if not evs:
            st.info("🎉 No meetings today!")
        else:
            with st.spinner("Generating preview…"):
                previews = []
                for ev in evs:
                    note = latest_granola_note(creds, ev.get("summary", ""), extract_emails(ev))
                    previews.append(summarise(ev, note))
                st.markdown("\n\n---\n\n".join(previews))

with tab_events:
    st.header("Today's meetings")
    if not creds or not creds.valid:
        st.info("Please sign in to view events.")
    else:
        events = fetch_todays_events(creds)
        if not events:
            st.info("No events found.")
        else:
            for ev in events:
                with st.container(border=True):
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        st.subheader(ev.get("summary", "Untitled"))
                        start = ev["start"].get("dateTime", ev["start"].get("date"))
                        st.write(f"🕒 {start}")
                        st.write(f"👥 {', '.join(extract_emails(ev))}")
                    with col2:
                        if st.button("Preview ➜", key=ev["id"]):
                            note = latest_granola_note(creds, ev.get("summary", ""), extract_emails(ev))
                            st.markdown(summarise(ev, note))

with tab_about:
    st.header("About this app")
    st.write(
        """
**Calendar Preread Assistant** automatically crafts and e‑mails a succinct
bullet‑point brief for each of your meetings, using:

* Google Calendar (to list events)
* Gmail (threads + latest Granola e‑mail)
* OpenAI GPT‑4o for summarisation

Source code is open‑source — PRs welcome!
"""
    )
