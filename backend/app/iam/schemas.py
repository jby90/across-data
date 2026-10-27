import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.iam.models import Role


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: Role
    org_id: uuid.UUID | None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID | None
    email: EmailStr
    display_name: str
    role: Role
    is_active: bool


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class OrgOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_active: bool
    llm_redaction_terms: list[str] = Field(default_factory=list)


class RedactionTermsIn(BaseModel):
    terms: list[str] = Field(max_length=100)

    @field_validator("terms")
    @classmethod
    def validate_terms(cls, terms):
        if any(not 2 <= len(v.strip()) <= 200 for v in terms):
            raise ValueError("每个脱敏词应为 2–200 字符")
        return list(dict.fromkeys(v.strip() for v in terms))


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=256)
    role: Role
    org_id: uuid.UUID | None = None


class MemberCreate(BaseModel):
    """企业管理员在本企业内新增成员。"""

    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=12, max_length=256)
    role: Role = Role.MEMBER
