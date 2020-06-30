import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

engine = create_engine(os.environ['SQLALCHEMY_URL'])

session = Session(bind=engine)
