import os
from datetime import datetime, timedelta, timezone

import jwt

SECRET_KEY = os.getenv("JWT_SECRET_KEY")
ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 7


def create_access_token(data: dict) -> str:
    pass


def create_refresh_token(data: dict) -> str:
    pass


def verify_access_token(token: str) -> dict:
    pass


def verify_refresh_token(token: str) -> dict:
    pass