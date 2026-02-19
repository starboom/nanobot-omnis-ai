"""Portal data models."""

from enum import Enum
from pydantic import BaseModel, Field


class UserRole(str, Enum):
    OWNER = "owner"
    USER = "user"
    ADMIN = "admin"


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=64)
    role: UserRole = UserRole.USER


class UserPublic(BaseModel):
    id: int
    username: str
    display_name: str
    role: UserRole
    created_at: str


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    user: UserPublic


class AgentListing(BaseModel):
    id: int
    owner_id: int
    employee_id: str
    employee_name: str
    skill: str
    price: int = 0
    description: str = ""
    listed: bool = True
    port: int = 0
    created_at: str


class TransactionType(str, Enum):
    TOPUP = "topup"
    PAYMENT = "payment"
    REFUND = "refund"


class Transaction(BaseModel):
    id: int
    from_user_id: int | None = None
    to_user_id: int | None = None
    amount: int
    tx_type: TransactionType
    listing_id: int | None = None
    memo: str = ""
    created_at: str
