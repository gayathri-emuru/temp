# Streamlit deployment

This app can run on Streamlit Community Cloud with `streamlit_app.py`.

## Deploy

1. Push this repository to GitHub.
2. Open Streamlit Community Cloud and create a new app from the repo.
3. Set the main file path to `streamlit_app.py`.
4. Add secrets in Streamlit app settings.

## Required secrets

```toml
SECRET_KEY = "change-this"
OPENAI_API_KEY = "sk-..."
ANTHROPIC_API_KEY = "sk-ant-..."
EMAIL_SENDING_ENABLED = "1"
EMAIL_SENDING_PAUSED = "0"
SEND_ATTACH_RESUME = "1"
SENDER_EMAIL = "emurugayathri@gmail.com"
SENDER_APP_PASSWORD = "your-gmail-app-password"
SENDER_DISPLAY_NAME = "Gayathri Emuru"
```

Add these only if the matching features are used:

```toml
APOLLO_API_KEY = "..."
APIFY_API_KEY = "..."
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = "587"
MICROSOFT_GRAPH_CLIENT_ID = "..."
MICROSOFT_GRAPH_TENANT = "consumers"
```

## Important

Do not make the GitHub repository public if it contains `db.sqlite3`, `.env`,
sender account passwords, OAuth tokens, resumes, or any private email data.
Streamlit secrets protect API keys, but files committed to a public repo are public.

The hosted Streamlit app is locked to `emurugayathri@gmail.com` for real sends.
It always attaches `resumes/Gayathri_Resume.pdf`; if that file is missing, sending fails.
The Inbox Monitor tab also scans only `emurugayathri@gmail.com` and requires the same Gmail app password/IMAP access.
