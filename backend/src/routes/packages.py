from fastapi import APIRouter,Depends,UploadFile,File
from fastapi.responses import Response
from src.config.database import get_db
from src.middlewares.auth import current_user,writer,admin
from src.controllers import packages
router=APIRouter()
@router.get('/packages/studies/{study_id}/export')
def export(study_id:int,db=Depends(get_db),user=Depends(writer)):
    raw,manifest=packages.export_package(db,study_id,user)
    name=f'research-package-{manifest["station_id"]}-{study_id}.zip'.replace(' ','_')
    return Response(content=raw,media_type='application/zip',
                    headers={'Content-Disposition':f'attachment; filename="{name}"'})
@router.post('/packages/preview')
async def preview(file:UploadFile=File(...),user=Depends(current_user)):
    return packages.preview_package(await file.read())
@router.post('/packages/import')
async def import_pack(file:UploadFile=File(...),user=Depends(admin)):
    return packages.import_package(await file.read(),user)
@router.get('/packages/imports')
def imports(db=Depends(get_db),user=Depends(current_user)):
    return packages.list_imports(db)
