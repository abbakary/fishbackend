from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .database import get_db
from .models import Tenant, User, AuditEvent
from .schemas import RegisterInput, LoginInput
from .security import create_access_token, hash_password, new_id

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post(
    "/register",
    response_model=LoginInput,
    status_code=status.HTTP_201_CREATED,
)
def register(
    payload: RegisterInput,
    db: Session = Depends(get_db),
):
    # 1️⃣ Validate uniqueness
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already exists",
        )

    # 2️⃣ Create tenant
    tenant = Tenant(
        id=new_id(),
        name=payload.company,
        plan=payload.plan,
        status="active",
    )
    db.add(tenant)

    # 3️⃣ Create admin user for tenant
    user = User(
        id=new_id(),
        username=payload.username,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role="Owner",
        tenant=tenant,
        active=True,
    )
    db.add(user)

    # 4️⃣ Audit event (FK satisfied because tenant & user exist)
    audit = AuditEvent(
        id=new_id(),
        tenant_id=tenant.id,
        user_id=user.id,
        action="tenant.registered",
        details={"branch_count": 1},
    )
    db.add(audit)

    db.commit()
    db.refresh(user)

    # 5️⃣ Return JWT (same shape as login)
    token = create_access_token(user.id, tenant.id)
    return {"access_token": token, "token_type": "bearer"}
