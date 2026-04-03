# Digital Behaviour Twin

AI-powered productivity tracking system with:

- web dashboard
- user auth and consent flow
- activity tracking and risk scoring
- connected device sync
- weekly reports and alerts
- admin panel

## Deploy As A Web System

This project is now prepared for deployment as a multi-user web system.

### What you need

- a Python web host such as Render
- a hosted MongoDB database such as MongoDB Atlas
- environment variables configured on the host

### Required environment variables

Use values similar to `.env.example`:

- `MONGODB_URI`
- `MONGODB_DB_NAME`
- `JWT_SECRET_KEY`
- `ADMIN_EMAILS`
- `API_BASE_URL`

Optional:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GROQ_API_KEY`
- `WHATSAPP_PHONE`
- `WHATSAPP_API_KEY`

### Render deployment

This repo includes:

- `requirements.txt`
- `Procfile`
- `render.yaml`

Start command:

```txt
gunicorn --chdir backend app:app --bind 0.0.0.0:$PORT
```

### Local production-like run

```bash
pip install -r requirements.txt
set MONGODB_URI=your_mongodb_uri
set JWT_SECRET_KEY=your_secret
gunicorn --chdir backend app:app --bind 0.0.0.0:5000
```

### Tracker note

If you want desktop activity tracking on other user systems, they must run the tracker separately on their machine and connect using the same deployed backend URL/config.

Set this for tracker-based clients:

- `API_BASE_URL=https://your-backend-service.onrender.com`
