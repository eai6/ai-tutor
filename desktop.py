import webview
import threading
from app import app, init_db

def start_server():
    init_db()  # Add this line
    app.run(port=8080, use_reloader=False)

if __name__ == '__main__':
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    webview.create_window('AI Tutor - TEVETA', 'http://localhost:8080', width=1200, height=800)
    webview.start()