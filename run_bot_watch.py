import subprocess
import sys
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import os

BOT_FILE = "bot.py"
WATCH_PATH = os.path.dirname(os.path.abspath(__file__))


class ReloadHandler(FileSystemEventHandler):
    def __init__(self, start_bot_func):
        super().__init__()
        self.start_bot_func = start_bot_func
        self.process = None
        self.bot_path = os.path.abspath(os.path.join(WATCH_PATH, BOT_FILE))

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Ne relance que si bot.py est modifié et event_type == 'modified'
        if os.path.abspath(event.src_path) == self.bot_path and event.event_type == "modified":
            print(f"[WATCH] Modification détectée: {event.src_path} (event: {event.event_type}). Reload...")
            self.restart_bot()

    def restart_bot(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
        self.process = self.start_bot_func()


def start_bot():
    print("[WATCH] Lancement du bot...")
    return subprocess.Popen([sys.executable, BOT_FILE])

if __name__ == "__main__":
    handler = ReloadHandler(start_bot)
    observer = Observer()
    observer.schedule(handler, WATCH_PATH, recursive=True)
    handler.process = start_bot()
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        if handler.process:
            handler.process.terminate()
    observer.join()
