from jwtauth.auth.token import verify_access_token
from fastapi import HTTPException
def get_user_details(token: str) -> dict:
    try:
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid user ID"
            )

        # Return user details
        return {
            "user_id": user_id,
        }

    except ValueError as e:
        raise HTTPException(
            status_code=401,
            detail=str(e)
        )
