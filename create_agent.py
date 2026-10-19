"""Create a human agent account for the Smile Dental Care reception console.

Prompts for a username, display name and password, then stores the account in
SQLite (``clinic.db``) with a securely hashed password and the ``agent`` role.

    python create_agent.py
    python create_agent.py --username riya --name "Riya" --password secret

Set ``DEMO_SEED=1`` when starting the app (or running ``db.init_db``) to create
two demo agent accounts automatically — never created otherwise.
"""

import os
import sys
import argparse

import db


def _prompt(label):
    """Prompt for a non-empty value (Ctrl-C / EOF aborts cleanly)."""
    while True:
        try:
            value = input(label).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if value:
            return value
        print("Please enter a value.")


def create_agent(username=None, display_name=None, password=None):
    """Create an agent account, prompting for anything not supplied."""
    db.init_db()  # ensure the schema (and migrations) exist

    username = username or _prompt("Agent username: ")
    display_name = display_name or _prompt("Display name: ")
    if not password:
        password = _prompt("Password: ")

    if db.get_admin(username):
        print("[!] An account named '%s' already exists." % username)
        return 1

    admin_id = db.create_admin(
        username, password, display_name=display_name, role="agent"
    )
    print("[ok] Created agent '%s' (id=%s, role=agent)." % (username, admin_id))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a reception agent account.")
    parser.add_argument("--username", help="agent login username")
    parser.add_argument("--name", dest="display_name", help="display name")
    parser.add_argument("--password", help="password (prompted if omitted)")
    args = parser.parse_args(argv)
    return create_agent(args.username, args.display_name, args.password)


if __name__ == "__main__":
    raise SystemExit(main())
