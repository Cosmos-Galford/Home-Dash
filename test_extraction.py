#!/usr/bin/env python3
"""
test_extraction.py - Try the extraction model on sample texts before trusting it.

    python3 test_extraction.py                    # uses OLLAMA_MODEL or gemma3:270m
    python3 test_extraction.py gemma3:1b          # compare another model

Run it next to llm_worker.py. Needs Ollama running locally and the model pulled.
Edit SAMPLES to add messages that look like yours (don't paste real private texts
into shared places). Output is what the worker would store, after validation.
"""
import os
import sys
import time
from datetime import date

os.environ["OLLAMA_MODEL"] = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("OLLAMA_MODEL", "gemma3:270m")
import llm_worker as w  # noqa: E402  (reads OLLAMA_MODEL at import time)

# (sender, text, expected type)
SAMPLES = [
    ("CVS", "CVS Pharmacy: Your prescription is ready for pickup at store #1234. Pick up by Friday.", "reminder"),
    ("CVS", "CVS: Time to refill your prescription. Reply 1 to refill or visit cvs.com/refill", "reminder"),
    ("Dr Smith Office", "Reminder: your appointment is tomorrow at 2:30 PM. Reply C to confirm.", "event"),
    ("Chase", "Your verification code is 482913. Do not share it with anyone.", "ignore"),
    ("Mom", "lol ok see you later!", "ignore"),
    ("Pet Supply", "FLASH SALE! 40% off everything today only. Reply STOP to opt out.", "ignore"),
    ("Electric Co", "Your bill of $84.20 is due on the 28th. Pay at example.com/pay", "reminder"),
]

received = date.today()
ok = 0
print(f"Model: {w.MODEL}   Today: {received.isoformat()}\n")
for sender, text, expected in SAMPLES:
    t0 = time.time()
    try:
        r = w.ask_model(sender, text, received)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {type(e).__name__}: {e}\n")
        continue
    dt = time.time() - t0
    got = r.get("type")
    d = w.clean_date(r.get("date"), received)
    mark = "OK " if got == expected else "MISS"
    ok += got == expected
    print(f"[{mark}] expected={expected:8} got={got:8} ({dt:.1f}s)")
    print(f"       text : {text[:70]}")
    print(f"       title: {r.get('title')!r}  date={d}  time={w.clean_time(r.get('time'))}  "
          f"urgency={r.get('urgency')}  conf={r.get('confidence')}\n")
print(f"{ok}/{len(SAMPLES)} classified as expected")
