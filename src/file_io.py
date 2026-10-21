"""Windows-safe log appends and main-thread whole-file replacement."""

import json
import os
import threading
import time


LOG_LOCK = threading.RLock()


def append_jsonl(path, event):
    line = json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
    with LOG_LOCK:
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(line)
            stream.flush()


def atomic_text(path, text):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Whole-file writes must run in the main thread')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.2)
