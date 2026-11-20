# -*- coding: utf-8 -*-
# Emergency tool: set a new password for a user (for example if the admin forgot it).
import getpass, hashlib, secrets, sqlite3
from pathlib import Path

db = Path(__file__).with_name("vouchers.db")
con = sqlite3.connect(db)
users = [r[0] for r in con.execute("SELECT username FROM users")]
print("Users:", ", ".join(users) if users else "(none)")
name = input("Username to reset: ").strip()
if name not in users:
    raise SystemExit("User not found.")
pw = getpass.getpass("New password (min 6 chars): ")
if len(pw) < 6:
    raise SystemExit("Password too short.")
salt = secrets.token_bytes(16)
h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
con.execute("UPDATE users SET pw_hash=? WHERE username=?", (f"pbkdf2${salt.hex()}${h.hex()}", name))
con.execute("DELETE FROM sessions WHERE username=?", (name,))
con.commit()
print("Done. You can log in with the new password now.")
