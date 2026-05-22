import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///econ.db")

engine = create_engine(DATABASE_URL)


class Base(DeclarativeBase):
    pass
