from fastapi import APIRouter, HTTPException
from jwtauth.auth.token import create_access_token, create_refresh_token
from users.signup.schemas.auth import UserSignupSchema
from sqlalchemy.orm import Session
from pwdlib import PasswordHash
from users.models.auth import User
password_hash = PasswordHash.recommended()
router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

def signup(user:UserSignupSchema,db:Session):
    existing_user=db.query(User).filter(User.email==user.email).first()
    if existing_user:
        raise HTTPException(
            status_code=409,
            detail="user already exist"   
        )
    hashed_password1=password_hash.hash(user.password)
    new_user=User(
        full_name=user.full_name,
        email=str(user.email),
        hashed_password=hashed_password1,
        phone_number=user.phone_number
    )
    try:
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="failed to register User"
        )
    access_token=create_access_token({"sub": str(new_user.id)})
    refresh_token=create_refresh_token({"sub": str(new_user.id)})
    return{
        "message": "Signup successful",
        "user_id": new_user.id,
        "full_name": new_user.full_name,
        "email": new_user.email,
        "ascess_token": access_token,
        "refresh_token": refresh_token
    }



    