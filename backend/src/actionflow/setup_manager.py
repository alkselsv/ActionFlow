import argparse
from getpass import getpass

from argon2 import PasswordHasher
from sqlalchemy import delete

from actionflow.config import Settings
from actionflow.persistence.database import LoginSession, User, database


def create_manager(sessions, username: str, password: str) -> None:
    password_hash = PasswordHasher().hash(password)
    with sessions.begin() as db:
        manager = db.get(User, 1)
        if manager is None:
            db.add(User(id=1, login=username, password_hash=password_hash))
        else:
            manager.login = username
            manager.password_hash = password_hash
            db.execute(delete(LoginSession).where(LoginSession.user_id == manager.id))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    args = parser.parse_args()
    username = args.username.strip()
    if not 1 <= len(username) <= 120:
        parser.error("Username must contain 1–120 characters")
    password = getpass("Пароль менеджера: ")
    if not 12 <= len(password) <= 256 or password != getpass("Повторите пароль: "):
        parser.error("Passwords must match and contain 12–256 characters")
    engine, sessions = database(Settings.from_env().database_url)
    try:
        create_manager(sessions, username, password)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
