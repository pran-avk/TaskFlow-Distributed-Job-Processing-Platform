from auth.token import verify_access_token
from fastapi import HTTPException
from jose.exceptions import ExpiredSignatureError, JWTError


def get_user_details(token: str) -> dict:
    try:
        # Verify the token and extract its payload
        payload = verify_access_token(token)

        # Extract user ID from the payload
        user_id = payload.get("sub")

        if user_id is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid user ID"
            )

        # Return user details
        return {
            "user_id": user_id,
            "payload": payload
        }

    except ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Access token expired"
        )

    except JWTError:
        raise HTTPException(
            status_code=401,
            detail="Invalid access token"
        )
