# 📆 Calendar Preread Assistant

*A one‑click Streamlit app that turns tomorrow’s Google Calendar into crisp morning briefings.*

> **What it does**
> 1. **Google OAuth login** – read today’s Calendar & Gmail, send e‑mail.
> 2. **Granola integration** – hunts for the most recent “Granola” e‑mail that matches the meeting title & attendees.
> 3. **OpenAI GPT‑4o summaries** – distills agenda, context, questions & logistics into neat bullets.
> 4. **Automatic 06:00 digest** – APScheduler mails you a single preread pack (or a “No meetings today 😊” note).
> 5. **On‑demand button** – test the workflow instantly from the UI.

---

## 🚀 Quick start (local)

```bash
git clone https://github.com/<you>/meeting-prereads.git.git
cd meeting-prereads.git
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GOOGLE_CLIENT_ID="xxxxxxxx.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="xxxxxxxxxxxxxxxxxxxx"
export OPENAI_API_KEY="sk-..."
# optional: change send time (CRON format)
export DAILY_PREREAD_CRON="0 7 * * *"   # 07:00 every morning

streamlit run app.py

When you first open the UI, click **Login with Google** and grant the requested read‑only & send scopes.

## Deployment (Streamlit Cloud)

1. Create a new Streamlit deployment pointing at `app.py`.
2. In **Secrets**, add:

```toml
google_client_id="<id>"
google_client_secret="<secret>"
openai_api_key="<openai>"
```

3. Set the app URL as an Authorised redirect URI for your Google OAuth client.

