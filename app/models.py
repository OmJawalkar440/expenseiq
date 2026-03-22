from sqlalchemy import Column, Integer, String, Float, DateTime
from datetime import datetime
from .database import Base

class ExpenseFile(Base):
    __tablename__ = "expense_files"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String)
    total_expense = Column(Float)
    upload_time = Column(DateTime, default=datetime.utcnow)