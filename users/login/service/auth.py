from fastapi import APIRouter, HTTPException
from jwtauth.auth.token import create_access_token, create_refresh_token
from users.login.schemas.auth import UserLoginSchema
router = APIRouter()
from sqlalchemy.orm import Session
from pwdlib import PasswordHash
from users.models.auth import User
password_hash = PasswordHash.recommended()

def login(user:UserLoginSchema,db:Session):
    existing_user=db.query(User).filter(User.email==user.email).first()
    if not existing_user:
        raise HTTPException(
            status_code=401,
            detail="Wrong password or invalid email"
        )

    if not password_hash.verify(user.password,existing_user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Wrong password or invalid email"
        )
    access_token=create_access_token({"sub": str(existing_user.id)})
    refreshtoken=create_refresh_token({"sub": str(existing_user.id)})
    return{
        "access_token": access_token,
        "refresh_token": refreshtoken
    }
