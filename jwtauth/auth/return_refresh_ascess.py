from fastapi import HTTPException
from jwtauth.auth.token import create_access_token, verify_refresh_token


def return_refresh_access(token: str) -> dict:
    try:
        # Verify the refresh token
        payload = verify_refresh_token(token)

        # Extract the user ID
        user_id = payload.get("sub")

        if user_id is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid user ID"
            )

        access_token = create_access_token({
            str(user_id)
        })

        
        return {
            "access_token": access_token,
            "token_type": "bearer"
        }

    except ValueError as e:
        raise HTTPException(
            status_code=401,
            detail=str(e)
        )