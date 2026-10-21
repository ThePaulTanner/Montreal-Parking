"""Manage logins on the NAS. Logins are never created through the web.

  docker exec -it mtl-parking-app python cli.py create-user --username paul
  docker exec -it mtl-parking-app python cli.py unlock
  docker exec -it mtl-parking-app python cli.py users | log | delete-user --username NAME
"""
import argparse
import getpass
import os
import sys
import time

from parking.accounts import AuthService

DATA = os.environ.get("DATA_DIR", "/data")


def service():
    os.makedirs(DATA, exist_ok=True)
    return AuthService(os.path.join(DATA, "auth.db"), os.environ.get("APP_SECRET", ""))


def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "never"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="add a login (prints an authenticator QR code and recovery codes)")
    c.add_argument("--username", required=True)
    sub.add_parser("unlock", help="clear the sign-in lock after too many failed attempts")
    sub.add_parser("users", help="list logins")
    sub.add_parser("log", help="recent sign-in attempts")
    d = sub.add_parser("delete-user", help="remove a login and its sessions")
    d.add_argument("--username", required=True)
    a = p.parse_args()
    svc = service()

    if a.cmd == "create-user":
        pw = getpass.getpass("Password (12+ characters): ")
        if pw != getpass.getpass("Again: "):
            sys.exit("The passwords differ.")
        try:
            secret, uri, codes = svc.create_user(a.username, pw)
        except ValueError as e:
            sys.exit(str(e))
        print("\nScan this with an authenticator app (Apple Passwords, 1Password, Authy...):\n")
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(uri)
            qr.print_ascii(invert=True)
        except Exception:
            pass
        print(f"\nCan't scan? Enter this key by hand: {secret}\n{uri}")
        print("\nRecovery codes. Store them in your password manager. Each works once and they are not shown again:\n")
        for code in codes:
            print("  " + code)
    elif a.cmd == "unlock":
        print("Unlocked." if svc.unlock() else "Sign-in was not locked.")
    elif a.cmd == "users":
        for u in svc.users():
            print(f"{u['username']:20} created {fmt(u['created_at'])}  last sign-in {fmt(u['last_sign_in'])}"
                  + ("  LOCKED" if u["locked_at"] else ""))
    elif a.cmd == "log":
        for r in svc.login_log(30):
            print(f"{fmt(r['ts'])}  {r['result']:13} {r['username'] or '-':14} {r['ip']:16} {r['device'] or ''}")
    elif a.cmd == "delete-user":
        print("Deleted." if svc.delete_user(a.username) else "No such login.")


if __name__ == "__main__":
    main()
