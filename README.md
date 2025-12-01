# NEU-Toronto-Chatbot

A simple Flask chatbot app.

## Quick Start (Windows / PowerShell)
1. cd ~\Documents\NEU-Toronto-Chatbot
2. py -m venv .venv
3. .\.venv\Scripts\Activate.ps1
4. pip install -r requirements.txt
5. (optional) set dev mode:   = 'development'
6. python app.py
7. Open http://127.0.0.1:5000 in your browser

## Project Structure
- app.py
- templates/
  - chat_v2.html
  - admin_v2.html
- static/   (images, css, js)
- requirements.txt
- .gitignore

## Troubleshooting
- If the venv won’t activate, run PowerShell as Administrator:
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
- If static files 404, ensure folder is **static** (lowercase) and templates use:
  {{ url_for('static', filename='img/monogram-with-background.png') }}
