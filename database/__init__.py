from . import database as db
from .database import *
import sys
sys.modules['database.db'] = db
