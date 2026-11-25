from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session, joinedload

from .database import get_db
from .models import Tenant, User
from .security import decode_access_token

bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user: User
    tenant: Tenant


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Principal:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        claims = decode_access_token(credentials.credentials)
    except Exception as exc:  # any bad/expired/malformed token is a 401
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token") from exc

    user_id, tenant_id = claims.get("sub"), claims.get("tid")
    if not user_id or not tenant_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = (
        db.query(User)
        .options(joinedload(User.tenant).joinedload(Tenant.branches))
        .filter(User.id == user_id, User.tenant_id == tenant_id, User.active.is_(True))
        .first()
    )
    if user is None or user.tenant is None or user.tenant.status == "suspended":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Account unavailable")
    return Principal(user=user, tenant=user.tenant)


def require_branch(principal: Principal, branch_id: str):
    branch = next((b for b in principal.tenant.branches if b.id == branch_id), None)
    if branch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Branch not found")
    allowed = set(principal.user.branch_ids or [])  # branch_ids can be None
    if principal.user.role not in {"Owner", "General Manager"} and branch_id not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Branch access denied")
    return branch