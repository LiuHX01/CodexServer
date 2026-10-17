"""Print the local admin access key in the user's terminal, never into a web page."""

from app.config import Settings

if __name__ == "__main__":
    print(Settings().admin_token.get_secret_value())
