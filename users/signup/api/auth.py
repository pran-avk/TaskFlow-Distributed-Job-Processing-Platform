from fastapi import Depends, HTTPException,Router
from sqlalchemy.orm import Session
from database import get_db

from users.signup.schemas.auth import UserSignupSchema
from users.signup.service.auth import signup
@Router.post("/signup")
def signup_user(user:UserSignupSchema,db:Session=Depends(get_db)):
    return signup(user, db)