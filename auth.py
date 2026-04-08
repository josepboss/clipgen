"""auth.py — Flask-Login setup and User model for ClipGen multi-user platform."""

from flask_login import LoginManager, UserMixin
from publisher_db import get_user_by_id

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to continue."
login_manager.login_message_category = "info"


class User(UserMixin):
    """Thin wrapper over the DB users row."""

    def __init__(self, user_id: int, username: str, email: str | None) -> None:
        self.id       = str(user_id)
        self.username = username
        self.email    = email or ""

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    row = get_user_by_id(int(user_id))
    if row:
        return User(row["id"], row["username"], row["email"])
    return None
