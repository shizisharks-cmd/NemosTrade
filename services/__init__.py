from . import tasks as background_tasks
from . import game_service
import sys
sys.modules['services.background_tasks'] = background_tasks
