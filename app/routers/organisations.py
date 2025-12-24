from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from typing import List

# Import our setup
from app.database import get_session
from app.models import Organisation, OrganisationBase, OrganisationUpdate

# Create a router specifically for organisations
router = APIRouter(prefix="/organisations", tags=["Organisations"])

# 1. Create an Organisation
@router.post("/", response_model=Organisation)
def create_organisation(org: OrganisationBase, session: Session = Depends(get_session)):
   db_org = Organisation.model_validate(org)
   session.add(db_org)
   session.commit()
   session.refresh(db_org)
   return db_org

# 2. Read All Organisations
@router.get("/", response_model=List[Organisation])
def read_organisations(session: Session = Depends(get_session)):
   organisations = session.exec(select(Organisation)).all()
   return organisations

# 3. Read One Organisation by ID
@router.get("/{org_id}", response_model=Organisation)
def read_organisation(org_id: int, session: Session = Depends(get_session)):
   org = session.get(Organisation, org_id)
   if not org:
       raise HTTPException(status_code=404, detail="Organisation not found")
   return org

# 4. Update an Organisation
@router.patch("/{org_id}", response_model=Organisation)
def update_organisation(org_id: int, org_update: OrganisationUpdate, session: Session = Depends(get_session)):
   db_org = session.get(Organisation, org_id)
   if not db_org:
       raise HTTPException(status_code=404, detail="Organisation not found")
   
   org_data = org_update.model_dump(exclude_unset=True)
   for key, value in org_data.items():
       setattr(db_org, key, value)
       
   session.add(db_org)
   session.commit()
   session.refresh(db_org)
   return db_org

# 5. Delete an Organisation
@router.delete("/{org_id}")
def delete_organisation(org_id: int, session: Session = Depends(get_session)):
   org = session.get(Organisation, org_id)
   if not org:
       raise HTTPException(status_code=404, detail="Organisation not found")
   session.delete(org)
   session.commit()
   return {"ok": True}