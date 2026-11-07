# Work Log Tracker

Live start/pause/resume/complete task timer + manager-ready daily summaries.
Login: hradmin / (the existing password).

## Local run

```bash
pip install -r requirements.txt
# optional: export DATABASE_URL=postgres://... (else uses local worklog.db)
streamlit run app.py
```

## Deploy (Render free tier)

- Build command: `pip install -r requirements.txt`
- Start command: `streamlit run app.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
- Set `DATABASE_URL` in Environment — **must** be a real Postgres connection
  string (Render's free tier disk is ephemeral; SQLite data would be lost on
  every redeploy).
