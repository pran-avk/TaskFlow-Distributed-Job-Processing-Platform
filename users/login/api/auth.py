from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import get_db
from jwtauth.auth.return_refresh_ascess import return_refresh_access
from users.login.schemas.auth import RefreshTokenSchema, UserLoginSchema
from users.login.service.auth import login

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

@router.post("/refresh")
def refresh_access_token(body: RefreshTokenSchema):
    return return_refresh_access(body.refresh_token)