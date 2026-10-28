from fastapi import APIRouter, Depends, HTTPException
from signup.schemas.auth import UserLoginSchema
router = APIRouter()
from sqlalchemy.orm import Session
from pwdlib import PasswordHash
from users.models.auth import User
password_hash = PasswordHash()

def login(user:UserLoginSchema,db:Session):
    existing_user=db.query(User).filter(User.email==user.email).first()
    if not existing_user:
        raise HTTPException(
            status_code=409,
            detail="Wrong password or invalid email"
        )

    if not password_hash.verify(user.password,existing_user.hashed_password):
        raise HTTPException(
            status_code=401,
            detail="Wrong password or invalid email"
        )
    return {
        "message":"login succesful",
    }

