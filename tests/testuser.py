from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from jwtauth.auth.returnuserdetails import get_user_details
from users.models.auth import User

router = APIRouter(
    prefix="/testuser",
    tags=["testuser"]
)

@router.post("/testUser")
def test_user(token: str, db: Session = Depends(get_db)):
    userdetails = get_user_details(token)
    userdet = db.query(User).filter(User.id == int(userdetails["user_id"])).first()
    if not userdet:
        raise HTTPException(status_code=404, detail="User not found")
    
    return userdetails["user_id"]
