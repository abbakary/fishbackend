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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    try:
        claims = decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    user = (
        db.query(User)
        .options(joinedload(User.tenant).joinedload(Tenant.branches))
        .filter(User.id == claims.get("sub"), User.tenant_id == claims.get("tid"), User.active.is_(True))
        .first()
    )
    if user is None or user.tenant.status == "suspended":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account unavailable")
    return Principal(user=user, tenant=user.tenant)


def require_branch(principal: Principal, branch_id: str):
    branch = next((b for b in principal.tenant.branches if b.id == branch_id), None)
    if branch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found")
    if principal.user.role not in {"Owner", "General Manager"} and branch_id not in principal.user.branch_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Branch access denied")
    return branch
