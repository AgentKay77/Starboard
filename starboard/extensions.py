"""Flask extensions instantiated once and shared across blueprints."""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager

limiter = Limiter(key_func=get_remote_address, default_limits=[])
login_manager = LoginManager()
