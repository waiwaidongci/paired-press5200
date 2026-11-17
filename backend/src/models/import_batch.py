from sqlalchemy import Column,Integer,String,JSON,Text
from src.config.database import Base
class ImportBatch(Base):
    # Migration ledger. One row per research package import attempt.
    # A staging row is committed before any business row is written, so a
    # crashed import keeps the same pre-allocated id mapping on recovery;
    # (source_station, source_study_id) is the same-source idempotency key.
    __tablename__='import_batches'
    id=Column(Integer,primary_key=True)
    source_station=Column(String(120),nullable=False)
    source_study_id=Column(Integer,nullable=False)
    package_checksum=Column(String(64),nullable=False)
    status=Column(String(20),default='staging')  # staging / completed / failed
    mapping=Column(JSON,nullable=False,default=dict)
    conflict_note=Column(Text,default='')
    error=Column(Text,default='')
    created_at=Column(String(40),nullable=False)
    finished_at=Column(String(40),nullable=True)
