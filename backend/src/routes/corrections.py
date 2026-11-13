from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..config.database import get_db
from ..controllers.dependencies import current_user, roles
from ..constants.roles import Role
from ..schemas.correction import CorrectionInput, CorrectionView
from ..services.correction_service import preview_correction, list_corrections, confirm_correction

router = APIRouter(tags=["corrections"])


@router.get("/studies/{sid}/corrections", response_model=list[CorrectionView])
def index(sid: int, db: Session = Depends(get_db), user=Depends(current_user)):
    return list_corrections(db, sid)


@router.post("/corrections/preview", response_model=CorrectionView, status_code=201)
def preview(data: CorrectionInput, db: Session = Depends(get_db),
            user=Depends(roles(Role.ADMIN, Role.ANALYST))):
    return preview_correction(db, data, user)


@router.get("/corrections", response_model=list[CorrectionView])
def all_corrections(db: Session = Depends(get_db), user=Depends(current_user)):
    return list_corrections(db)


@router.post("/corrections/{cid}/confirm", response_model=CorrectionView)
def confirm(cid: int, db: Session = Depends(get_db),
            user=Depends(roles(Role.ADMIN, Role.ANALYST))):
    return confirm_correction(db, cid, user)
