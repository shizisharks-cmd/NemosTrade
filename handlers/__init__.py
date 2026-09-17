from . import user as user_handlers
from . import game as game_handlers
from . import admin as admin_handlers
import sys
sys.modules['handlers.user_handlers'] = user_handlers
sys.modules['handlers.game_handlers'] = game_handlers
sys.modules['handlers.admin_handlers'] = admin_handlers
