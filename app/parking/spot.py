"""Where the car is parked: one saved spot per login.

It lives in the same SQLite file as the logins and is tied to the login with ON DELETE CASCADE, so deleting a
login deletes its spot, and a later login that happens to reuse the id never sees someone else's car."""
import math
import sqlite3
import threading
import time

NOTE_MAX = 120


class SpotStore:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS parked(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,"
            " lat REAL NOT NULL, lon REAL NOT NULL, accuracy REAL, street TEXT, note TEXT NOT NULL DEFAULT '',"
            " parked_at REAL NOT NULL)")

    @staticmethod
    def _clean_note(note):
        return "".join(ch for ch in (note or "") if ch.isprintable()).strip()[:NOTE_MAX]

    def get(self, user_id):
        with self._lock:  # one connection is shared by the worker threads, so reads wait for writes too
            row = self.db.execute("SELECT * FROM parked WHERE user_id=?", (user_id,)).fetchone()
        return self._out(row) if row else None

    def set(self, user_id, lat, lon, accuracy=None, street=None, note=""):
        """Save (or replace) the spot. Raises ValueError for a position that cannot be real."""
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("That is not a valid position.")
        if accuracy is not None and not (math.isfinite(accuracy) and 0 <= accuracy <= 100_000):
            accuracy = None
        with self._lock:
            try:
                self.db.execute(
                    "INSERT INTO parked(user_id, lat, lon, accuracy, street, note, parked_at) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, accuracy=excluded.accuracy,"
                    " street=excluded.street, note=excluded.note, parked_at=excluded.parked_at",
                    (user_id, float(lat), float(lon), accuracy, (street or None), self._clean_note(note), self.clock()))
            except sqlite3.IntegrityError:
                raise ValueError("No such login.") from None
            return self.get(user_id)

    def set_note(self, user_id, note):
        with self._lock:
            n = self.db.execute("UPDATE parked SET note=? WHERE user_id=?", (self._clean_note(note), user_id)).rowcount
        return self.get(user_id) if n else None

    def clear(self, user_id):
        with self._lock:
            return self.db.execute("DELETE FROM parked WHERE user_id=?", (user_id,)).rowcount == 1

    @staticmethod
    def _out(row):
        return {"lat": row["lat"], "lon": row["lon"], "accuracy_m": row["accuracy"], "street": row["street"],
                "note": row["note"],
                "parked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["parked_at"]))}
