from fastapi import Depends, APIRouter
from sqlalchemy.orm import Session
from database import get_db

from users.signup.schemas.auth import UserSignupSchema
from users.signup.service.auth import signup

Router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

@Router.post("/signup")
def signup_user(user:UserSignupSchema,db:Session=Depends(get_db)):
    return signup(user, db)