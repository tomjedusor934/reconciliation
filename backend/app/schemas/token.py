from typing import Optional

from pydantic import BaseModel


class Token(BaseModel):
    access_token: str
    token_type: str

class TokenPayload(BaseModel):
    sub: Optional[str] = None
    type: Optional[str] = None

class LoginResponse(BaseModel):
    message: str
    user: dict
    password_days_remaining: Optional[int] = None
    must_change_password: bool = False
    is_blocked: bool = False
    remaining_attempts: Optional[int] = None
