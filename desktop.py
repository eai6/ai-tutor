#!/usr/bin/env python3
"""
AI Tutor Desktop Application
============================
Cross-platform desktop wrapper using pywebview.
Runs the Flask app in a native window.
"""

import os
import sys
import threading
import webview
from pathlib import Path

# Determine if running as frozen executable (PyInstaller)
if getattr(sys, 'frozen', False):
    # Running as compiled executable
    BASE_DIR = Path(sys._MEIPASS)
    os.chdir(BASE_DIR)
else:
    # Running as script
    BASE_DIR = Path(__file__).parent

# Set environment variables
os.environ.setdefault('FLASK_ENV', 'production')

# Import the Flask app
from app import app

# Configuration
WINDOW_TITLE = "AI Tutor - TEVET Skills Development"
WINDOW_WIDTH = 1400
WINDOW_HEIGHT = 900
MIN_WIDTH = 800
MIN_HEIGHT = 600


def get_config():
    """Load configuration from config file or environment."""
    config_path = BASE_DIR / 'config' / 'settings.json'
    
    config = {
        'title': WINDOW_TITLE,
        'width': WINDOW_WIDTH,
        'height': WINDOW_HEIGHT,
        'min_width': MIN_WIDTH,
        'min_height': MIN_HEIGHT,
    }
    
    if config_path.exists():
        import json
        with open(config_path) as f:
            file_config = json.load(f)
            config.update(file_config.get('window', {}))
    
    return config


def run_flask():
    """Run Flask app in a separate thread."""
    # Disable Flask's reloader in desktop mode
    app.run(
        host='127.0.0.1',
        port=5000,
        debug=False,
        use_reloader=False,
        threaded=True
    )


def main():
    """Main entry point for desktop application."""
    print(f"Starting AI Tutor Desktop...")
    print(f"Base directory: {BASE_DIR}")
    
    # Get window configuration
    config = get_config()
    
    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Give Flask a moment to start
    import time
    time.sleep(1)
    
    # Create webview window
    window = webview.create_window(
        title=config['title'],
        url='http://127.0.0.1:5000',
        width=config['width'],
        height=config['height'],
        min_size=(config['min_width'], config['min_height']),
        resizable=True,
        fullscreen=False,
        frameless=False,
        easy_drag=False,
        text_select=True,
    )
    
    # Start webview (blocking)
    webview.start(
        debug=False,
        http_server=False,
    )
    
    print("AI Tutor Desktop closed.")


if __name__ == '__main__':
    main()
