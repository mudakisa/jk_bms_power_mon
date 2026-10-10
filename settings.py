"""PowerMon settings: config.json in the project root (cp config.example.json config.json).

    {"db": "powermon.db",
     "banks": [{"id": "bank1", "address": "AA:BB:CC:DD:EE:FF", "name": "House"}]}

"db" is the SQLite file (relative = to the project folder); "name" (optional) is what the
dashboards put on the bank's button. An older config.py still works when there is no
config.json.
"""
import json, os

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load():
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path) as f:
            c = json.load(f)
        db = c.get("db", "powermon.db")
        banks = [(b["id"], b["address"], b.get("name", b["id"])) for b in c["banks"]]
    else:
        try:
            from config import DB as db, BANKS as banks     # the pre-JSON config.py
        except ImportError:
            raise SystemExit("PowerMon: no config.json in the project root.\n"
                             "    cp config.example.json config.json   # then edit it")
    return (db if os.path.isabs(db) else os.path.join(ROOT, db)), list(banks)


DB, BANKS = _load()
