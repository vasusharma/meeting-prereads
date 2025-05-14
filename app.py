
import streamlit as st
import os
import datetime as dt
from pathlib import Path
from typing import List, Dict
import openai
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from apscheduler.schedulers.background import BackgroundScheduler
import base64
import email
import json
import yarl

# -----------------------------
# CONFIGURATION
# -----------------------------
SCOPES = [
    'https://www.googleapis.com/auth/calendar.readonly',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.send'
]

# Expect these secrets in your environment or Streamlit secrets
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')

# # For Streamlit Cloud put them in .streamlit/secrets.toml
if 'openai_api_key' in st.secrets:
    OPENAI_API_KEY = st.secrets['openai_api_key']
if 'google_client_id' in st.secrets:
    GOOGLE_CLIENT_ID = st.secrets['google_client_id']
if 'google_client_secret' in st.secrets:
    GOOGLE_CLIENT_SECRET = st.secrets['google_client_secret']

openai.api_key = OPENAI_API_KEY

# -----------------------------
# AUTH HELPERS
# -----------------------------
def save_credentials_to_session(creds: Credentials):
    st.session_state['token'] = creds.to_json()

def get_credentials() -> Credentials | None:
    if 'token' in st.session_state:
        return Credentials.from_authorized_user_info(json.loads(st.session_state['token']), SCOPES)
    token_path = Path('token.json')
    if token_path.exists():
        with open(token_path, 'r') as f:
            return Credentials.from_authorized_user_info(json.load(f), SCOPES)
    return None

def store_credentials(creds: Credentials):
    with open('token.json', 'w') as f:
        f.write(creds.to_json())
    save_credentials_to_session(creds)

# -------------------- AUTH HELPERS --------------------
REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8501/auth_callback"      # <- fallback for local dev
)

def login():
    flow = Flow.from_client_config(
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

    auth_url, _ = flow.authorization_url(
        prompt="consent", access_type="offline", include_granted_scopes="true"
    )
    st.markdown(f"[**Login with Google**]({auth_url})")

def handle_auth_callback():
    params = st.query_params
    if "code" not in params:
        return
    flow = Flow.from_client_config(
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
    flow.fetch_token(code=params["code"])
    creds = flow.credentials
    store_credentials(creds)
    st.query_params.clear()          # wipe ?code=...
    st.rerun()


# -----------------------------
# DATA FETCHING
# -----------------------------
def fetch_todays_events(creds: Credentials) -> List[Dict]:
    service = build('calendar', 'v3', credentials=creds)
    now = dt.datetime.utcnow().isoformat() + 'Z'
    tomorrow = (dt.datetime.utcnow() + dt.timedelta(days=1)).isoformat() + 'Z'
    events_result = service.events().list(calendarId='primary', timeMin=now,
                                          timeMax=tomorrow, singleEvents=True,
                                          orderBy='startTime').execute()
    return events_result.get('items', [])

def extract_emails_from_event(event: Dict) -> List[str]:
    emails = []
    attendees = event.get('attendees', [])
    for att in attendees:
        if att.get('email'):
            emails.append(att['email'])
    creator = event.get('creator', {}).get('email')
    if creator:
        emails.append(creator)
    return list(set(emails))

def fetch_threads_for_emails(creds: Credentials, emails: List[str]) -> List[email.message.EmailMessage]:
    gmail_service = build('gmail', 'v1', credentials=creds)
    messages = []
    for mail in emails:
        try:
            query = f'from:{mail} OR to:{mail}'
            resp = gmail_service.users().messages().list(userId='me', q=query, maxResults=10).execute()
            for msg in resp.get('messages', []):
                msg_detail = gmail_service.users().messages().get(userId='me', id=msg['id'], format='raw').execute()
                msg_raw = base64.urlsafe_b64decode(msg_detail['raw'])
                messages.append(email.message_from_bytes(msg_raw))
        except HttpError as e:
            st.error(f'Error fetching Gmail threads: {e}')
    return messages

def fetch_granola_notes(creds: Credentials, meeting_title: str, email_ids: List[str]) -> str:
    gmail_service = build('gmail', 'v1', credentials=creds)

    query_parts = [f'"{meeting_title}"', 'Granola']
    for email in email_ids:
        query_parts.append(f'from:{email} OR to:{email}')
    query = ' '.join(query_parts)

    try:
        results = gmail_service.users().messages().list(
            userId='me',
            q=query,
            maxResults=5,
            labelIds=["INBOX"]
        ).execute()

        messages = results.get('messages', [])
        if not messages:
            return "No prior Granola notes found."

        # Get the most recent matching message
        msg_id = messages[0]['id']
        msg_detail = gmail_service.users().messages().get(
            userId='me', id=msg_id, format='full'
        ).execute()

        payload = msg_detail.get('payload', {})
        parts = payload.get('parts', [])
        body = ""

        if parts:
            for part in parts:
                if part['mimeType'] == 'text/plain':
                    body = base64.urlsafe_b64decode(part['body']['data']).decode()
                    break
        else:
            body = base64.urlsafe_b64decode(payload['body']['data']).decode()

        # Trim to a reasonable length
        body = body.strip()
        if len(body) > 2000:
            body = body[:2000] + '...'

        return f"Prior Granola Note:\n{body}"

    except Exception as e:
        return f"⚠️ Error fetching Granola notes: {e}"


# -----------------------------
# SUMMARIZATION
# -----------------------------
from openai import OpenAI

client = OpenAI(api_key=OPENAI_API_KEY)

def summarize_meeting(event: Dict, emails: List[email.message.EmailMessage], granola_notes: str) -> str:
    start = event['start'].get('dateTime', event['start'].get('date'))
    attendees = ', '.join(extract_emails_from_event(event))
    email_snippets = '\n'.join([msg.get('Subject', '') for msg in emails[:5]])

    prompt = f"""You are an executive assistant. Create a concise, actionable preread for the following meeting.
MEETING TITLE: {event.get('summary')}
START: {start}
ATTENDEES: {attendees}
EMAIL THREAD CONTEXT (subjects only):
{email_snippets}
ADDITIONAL NOTES:
{granola_notes}

Structure the preread in bullets under the headings: Objective, Key Context, Questions / Decisions, Logistics.
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    return response.choices[0].message.content.strip()

# -----------------------------
# EMAIL SENDING
# -----------------------------
def send_email(creds: Credentials, to_email: str, subject: str, body: str):
    try:
        gmail_service = build("gmail", "v1", credentials=creds)

        # If the id_token is missing (offline refresh token flow) call userinfo
        sender = creds.id_token.get("email") if creds.id_token else None
        if not sender:
            oauth2 = build("oauth2", "v2", credentials=creds)
            sender = oauth2.userinfo().get().execute()["email"]

        msg = email.message.EmailMessage()
        msg["To"] = to_email or sender
        msg["From"] = sender
        msg["Subject"] = subject
        msg.set_content(body)

        encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        resp = gmail_service.users().messages().send(
            userId="me", body={"raw": encoded}
        ).execute()

        st.success(f"✉️  Gmail accepted message id: {resp['id']}")
        return True

    except HttpError as e:
        st.error(f"Gmail API error — {e}")
        return False
    except Exception as ex:
        st.error(f"Unexpected error — {ex}")
        return False



# -----------------------------
# DAILY JOB
# -----------------------------
def daily_preread_job():
    creds = get_credentials()
    if not creds:
        return
    events = fetch_todays_events(creds)
    st.write(f"📅 fetched {len(events)} events")
    summaries = []
    for ev in events:
        emails = fetch_threads_for_emails(creds, extract_emails_from_event(ev))
        granola_notes = fetch_granola_notes(creds, ev.get("summary", ""), extract_emails_from_event(ev))
        summaries.append(summarize_meeting(ev, emails, granola_notes))
    try:
        # Determine who to send to
        # sender = creds.id_token.get("email") if creds.id_token else None
        sender = None
        # if not sender:
        #     oauth2 = build("oauth2", "v2", credentials=creds)
        #     sender = oauth2.userinfo().get().execute()["email"]
        if not sender:
            profile = build("gmail", "v1", credentials=creds).users().getProfile(userId="me").execute()
            sender = profile.get("emailAddress")


        if summaries:
            body = "\n\n---\n\n".join(summaries)
            subject = "Daily Meeting Prereads"
        else:
            body = (
                "You're all clear today! 🎉\n\n"
                "No meetings were found on your calendar.\n"
                "Enjoy your day!"
            )
            subject = "No Meetings Today 😊"

        send_email(creds, sender, subject, body)
        st.success(f"✅ Email sent: {subject}")

    except Exception as e:
        st.error(f"Error while sending email: {e}")


# Kick off scheduler once per session
if 'scheduler_started' not in st.session_state:
    sched = BackgroundScheduler()
    # Run every day at 06:00 local server time
    sched.add_job(daily_preread_job, 'cron', hour=6, minute=0)
    sched.start()
    st.session_state['scheduler_started'] = True

# -----------------------------
# STREAMLIT UI
# -----------------------------
st.set_page_config(page_title='Calendar Preread Assistant', page_icon='📆')
st.title('📆 Calendar Preread Assistant')

handle_auth_callback()
creds = get_credentials()
if not creds or not creds.valid:
    login()
    st.stop()

st.success('✅ Logged in successfully!')

if st.button('Run daily preread job now'):
    daily_preread_job()
    st.success('Sent!')

# Display todays events + prereads in the UI
events = fetch_todays_events(creds)
for ev in events:
    st.subheader(ev.get('summary', 'No Title'))
    with st.expander('Details'):
        st.json(ev)
    emails = fetch_threads_for_emails(creds, extract_emails_from_event(ev))
    granola = fetch_granola_notes(creds, ev.get("summary", ""), extract_emails_from_event(ev))
    summary = summarize_meeting(ev, emails, granola)
    st.markdown(summary)
# placeholder code, see chat
