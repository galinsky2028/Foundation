#!/usr/bin/env python3
"""Project commands.

  python scripts/manage.py migrate                       apply all migrations
  python scripts/manage.py makemigration "message"       new migration for app/models.py changes
  python scripts/manage.py createadmin EMAIL             create an administrator (asks for a password)
  python scripts/manage.py apikey NAME [SCOPE ...]       create an API key and print it once
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]
    from foundation.db import SessionLocal, engine, init_db

    if cmd == "migrate":
        init_db()
        print("migrations applied")
    elif cmd == "makemigration":
        import foundation.models  # noqa: F401
        import app.models  # noqa: F401
        from foundation.migrate import make_migration

        branch = "foundation" if "--foundation" in args else "app"
        make_migration(engine, " ".join(a for a in args if a != "--foundation") or "update", branch)
    elif cmd == "createadmin":
        init_db()
        from foundation.auth_core import create_user, get_user_by_email
        from foundation.security import password_problem

        password = getpass.getpass("Password: ")
        if problem := password_problem(password):
            print(problem)
            return 1
        with SessionLocal() as s:
            if get_user_by_email(s, args[0]):
                print("That user already exists")
                return 1
            create_user(s, args[0], password, role="admin")
            s.commit()
        print(f"administrator {args[0]} created")
    elif cmd == "apikey":
        init_db()
        from foundation.auth_core import create_api_key

        print(create_api_key(args[0], args[1:] or ["*"]))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
