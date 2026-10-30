from fastapi import FastAPI, HTTPException

from database import Base, engine
from users.models.auth import User
from users.signup.api.auth import Router as signup_router
from users.login.api.auth import router as login_router

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.include_router(signup_router)
app.include_router(login_router)
