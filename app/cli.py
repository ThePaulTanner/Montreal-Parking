"""Manage logins on the NAS. Logins and password resets are never done through the web.

  docker exec -it mtl-parking-app python cli.py create-user --username paul
  docker exec -it mtl-parking-app python cli.py reset-password --username paul
  docker exec -it mtl-parking-app python cli.py remove-authenticator --username paul   (lost the authenticator app)
  docker exec -it mtl-parking-app python cli.py add-authenticator --username paul      (optional extra factor)
  docker exec -it mtl-parking-app python cli.py unlock | users | log | delete-user --username NAME

A login is a password (14+ characters) plus passkeys you add in Settings. The authenticator is optional.
"""
import argparse
import getpass
import os
import sys
import time

from parking.accounts import MIN_PASSWORD_LENGTH_ALONE, AuthService

DATA = os.environ.get("DATA_DIR", "/data")


def service():
    os.makedirs(DATA, exist_ok=True)
    return AuthService(os.path.join(DATA, "auth.db"), os.environ.get("APP_SECRET", ""))


def fmt(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "never"


def ask_password(prompt="Password"):
    pw = getpass.getpass(f"{prompt} ({MIN_PASSWORD_LENGTH_ALONE}+ characters, a few random words is fine): ")
    if pw != getpass.getpass("Again: "):
        sys.exit("The passwords differ.")
    return pw


def show_authenticator(secret, uri, codes):
    print("\nScan this with an authenticator app (Apple Passwords, 1Password, Authy...):\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print(f"\nCan't scan? Enter this key by hand: {secret}\n{uri}")
    print("\nRecovery codes. Store them somewhere other than your phone. Each works once and they are not shown again:\n")
    for code in codes:
        print("  " + code)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-user", help="add a login (password; add passkeys in Settings after signing in)")
    c.add_argument("--username", required=True)
    c.add_argument("--with-authenticator", action="store_true",
                   help="also require an authenticator code (prints a QR code and 10 recovery codes)")
    for name, text in (("reset-password", "set a new password (signs the login out everywhere, keeps passkeys)"),
                       ("remove-authenticator", "drop a lost authenticator; asks for a new password"),
                       ("add-authenticator", "add or replace the authenticator and its recovery codes"),
                       ("delete-user", "remove a login, its passkeys and sessions")):
        sub.add_parser(name, help=text).add_argument("--username", required=True)
    sub.add_parser("unlock", help="clear the sign-in lock after too many failed attempts")
    sub.add_parser("users", help="list logins")
    sub.add_parser("log", help="recent sign-in attempts")
    a = p.parse_args(argv)
    svc = service()

    if a.cmd == "create-user":
        pw = getpass.getpass(f"Password ({12 if a.with_authenticator else MIN_PASSWORD_LENGTH_ALONE}+ characters): ")
        if pw != getpass.getpass("Again: "):
            sys.exit("The passwords differ.")
        try:
            secret, uri, codes = svc.create_user(a.username, pw, authenticator=a.with_authenticator)
        except ValueError as e:
            sys.exit(str(e))
        if secret:
            show_authenticator(secret, uri, codes)
        print(f"\nLogin '{a.username.strip().lower()}' created. Sign in with the password, then open Settings and set up a "
              "passkey (Face ID) on each device.")
    elif a.cmd == "reset-password":
        try:
            ok = svc.set_password(a.username, ask_password("New password"))
        except ValueError as e:
            sys.exit(str(e))
        print("Password changed. The login was signed out everywhere." if ok else "No such login.")
    elif a.cmd == "remove-authenticator":
        print("Without the authenticator, the password alone protects this login, so choose a new one "
              f"of at least {MIN_PASSWORD_LENGTH_ALONE} characters (it can be the same words with more added).")
        try:
            ok = svc.remove_authenticator(a.username, ask_password("New password"))
        except ValueError as e:
            sys.exit(str(e))
        print("Authenticator removed. Sign in with the new password." if ok else "No such login.")
    elif a.cmd == "add-authenticator":
        got = svc.add_authenticator(a.username)
        if got is None:
            sys.exit("No such login.")
        show_authenticator(*got)
        print("\nThe login was signed out everywhere. From now on it needs an authenticator code too.")
    elif a.cmd == "unlock":
        print("Unlocked." if svc.unlock() else "Sign-in was not locked.")
    elif a.cmd == "users":
        for u in svc.users():
            print(f"{u['username']:20} created {fmt(u['created_at'])}  last sign-in {fmt(u['last_sign_in'])}  "
                  f"authenticator {'on' if u['authenticator'] else 'off'}  passkeys {u['passkeys']}"
                  + ("  LOCKED" if u["locked_at"] else ""))
    elif a.cmd == "log":
        for r in svc.login_log(30):
            print(f"{fmt(r['ts'])}  {r['result']:21} {r['username'] or '-':14} {r['ip']:16} {r['device'] or ''}")
    elif a.cmd == "delete-user":
        print("Deleted." if svc.delete_user(a.username) else "No such login.")


if __name__ == "__main__":
    main()
