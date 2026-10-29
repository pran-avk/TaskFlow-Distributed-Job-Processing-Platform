from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from signup.schemas.auth import UserLoginSchema
from service.auth import login

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

@router.post("/login")
def login_user(
    user: UserLoginSchema,
    db: Session = Depends(get_db)
):
    return login(user, db)