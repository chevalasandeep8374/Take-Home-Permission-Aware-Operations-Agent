from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Union
import json
import jwt
from fastapi import Depends, HTTPException, status, APIRouter, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db

auth_router = APIRouter(tags=["Authentication"])
security = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    """
    Authenticated user identity and effective permission context.
    Built strictly from session token and database, never from LLM prompts.
    """
    user_id: int
    hospital_id: int
    roles: List[str] = field(default_factory=list)
    permissions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    is_active: bool = True
    name: Optional[str] = None
    email: Optional[str] = None

    def has_permission(self, code: str) -> bool:
        return code in self.permissions

    def get_constraint(self, code: str, key: str, default: Any = None) -> Any:
        return self.permissions.get(code, {}).get(key, default)

    @property
    def is_admin(self) -> bool:
        return "Admin" in self.roles or "admin" in self.roles


class LoginRequest(BaseModel):
    email: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    hospital_id: int
    roles: List[str]


def create_access_token(user_id: int, hospital_id: int, expires_delta: Optional[timedelta] = None) -> str:
    """Issue a signed JWT session token."""
    now = datetime.now(timezone.utc)
    expire = now + (expires_delta or timedelta(hours=8))
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "hospital_id": hospital_id,
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Verify and decode a JWT session token."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token has expired. Please log in again."
        )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid session token: {str(e)}"
        )


def _parse_json_field(val: Any) -> Any:
    if val is None:
        return {}
    if isinstance(val, (dict, list)):
        return val
    try:
        return json.loads(val)
    except (TypeError, ValueError):
        return {}


def fetch_principal_from_db(identifier: Union[int, str], db: Session) -> Optional[Principal]:
    """
    Look up user and compute effective permissions from database.
    Effective permissions = role permissions + 'grant' overrides - 'deny'/'revoke' overrides.
    Uses the auth_lookup(:param) function from 01_schema.sql with direct query fallback.
    """
    param_str = str(identifier)

    # 1. Try calling the auth_lookup stored function
    try:
        query = text("""
            SELECT user_id, hospital_id, name, email, is_active, roles, effective_permissions
            FROM auth_lookup(:identifier);
        """)
        row = db.execute(query, {"identifier": param_str}).fetchone()
        if row:
            roles = _parse_json_field(row.roles)
            if not isinstance(roles, list):
                roles = [roles] if roles else []
            perms = _parse_json_field(row.effective_permissions)
            if not isinstance(perms, dict):
                perms = {}
            return Principal(
                user_id=row.user_id,
                hospital_id=row.hospital_id,
                name=row.name,
                email=row.email,
                is_active=bool(row.is_active),
                roles=roles,
                permissions=perms,
            )
    except Exception:
        db.rollback()

    # 2. Fallback CTE query if auth_lookup function is not yet present
    fallback_query = text("""
        WITH user_data AS (
            SELECT u.id, u.hospital_id, u.name, u.email, u.is_active
            FROM users u
            WHERE u.email = :identifier OR u.id::TEXT = :identifier
        ),
        user_role_names AS (
            SELECT ur.user_id, jsonb_agg(DISTINCT r.name) AS roles
            FROM user_roles ur
            JOIN roles r ON ur.role_id = r.id
            WHERE ur.user_id = (SELECT id FROM user_data)
            GROUP BY ur.user_id
        ),
        role_perms AS (
            SELECT p.code, rp.constraints
            FROM user_roles ur
            JOIN role_permissions rp ON ur.role_id = rp.role_id
            JOIN permissions p ON rp.permission_id = p.id
            WHERE ur.user_id = (SELECT id FROM user_data)
        ),
        grant_overrides AS (
            SELECT p.code, upo.constraints
            FROM user_permission_overrides upo
            JOIN permissions p ON upo.permission_id = p.id
            WHERE upo.user_id = (SELECT id FROM user_data)
              AND upo.effect = 'grant'
        ),
        deny_overrides AS (
            SELECT p.code
            FROM user_permission_overrides upo
            JOIN permissions p ON upo.permission_id = p.id
            WHERE upo.user_id = (SELECT id FROM user_data)
              AND upo.effect IN ('deny', 'revoke')
        ),
        combined_perms AS (
            SELECT code, constraints FROM role_perms
            UNION ALL
            SELECT code, constraints FROM grant_overrides
        ),
        final_perms AS (
            SELECT code, constraints
            FROM combined_perms
            WHERE code NOT IN (SELECT code FROM deny_overrides)
        )
        SELECT 
            ud.id AS user_id,
            ud.hospital_id,
            ud.name,
            ud.email,
            ud.is_active,
            COALESCE(urn.roles, '[]'::jsonb) AS roles,
            COALESCE(
                (SELECT jsonb_object_agg(code, constraints) FROM final_perms),
                '{}'::jsonb
            ) AS effective_permissions
        FROM user_data ud
        LEFT JOIN user_role_names urn ON ud.id = urn.user_id;
    """)

    row = db.execute(fallback_query, {"identifier": param_str}).fetchone()
    if not row:
        return None

    roles = _parse_json_field(row.roles)
    if not isinstance(roles, list):
        roles = [roles] if roles else []
    perms = _parse_json_field(row.effective_permissions)
    if not isinstance(perms, dict):
        perms = {}

    return Principal(
        user_id=row.user_id,
        hospital_id=row.hospital_id,
        name=row.name,
        email=row.email,
        is_active=bool(row.is_active),
        roles=roles,
        permissions=perms,
    )


def get_current_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Principal:
    """
    FastAPI dependency that constructs the authenticated Principal ONLY from
    the validated session JWT and database lookup.
    Never accepts or trusts identity from user prompt or request body.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header with Bearer token."
        )

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("user_id") or payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token missing user identification payload."
        )

    principal = fetch_principal_from_db(int(user_id), db)
    if not principal:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User referenced in session token does not exist."
        )

    if not principal.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive. Access denied."
        )

    return principal


@auth_router.post("/login", response_model=TokenResponse)
def login(
    req: Optional[LoginRequest] = None,
    email: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Demo login endpoint: issues a session JWT for an email.
    Accepts either POST body {"email": "..."} or query parameter ?email=...
    """
    user_email = (req.email if req and req.email else email)
    if not user_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email address is required."
        )

    principal = fetch_principal_from_db(user_email.strip(), db)
    if not principal:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with email '{user_email}' not found."
        )

    token = create_access_token(user_id=principal.user_id, hospital_id=principal.hospital_id)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        user_id=principal.user_id,
        hospital_id=principal.hospital_id,
        roles=principal.roles,
    )
