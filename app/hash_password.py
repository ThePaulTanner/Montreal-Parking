"""Print a password hash for APP_PASSWORD_HASH. Usage: python hash_password.py"""
import getpass

from parking.auth import hash_password

pw = getpass.getpass("Password: ")
if pw != getpass.getpass("Again: "):
    raise SystemExit("Passwords differ")
print(hash_password(pw))
