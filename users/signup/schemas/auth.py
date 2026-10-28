from pydantic import BaseModel, EmailStr, Field

class UserSignupSchema(BaseModel):
    email: EmailStr = Field(..., description="User's email address")
    password: str = Field(..., min_length=8, description="User's password")
    full_name: str = Field(..., description="User's full name")
    phone_number: str = Field(..., description="User's phone number")