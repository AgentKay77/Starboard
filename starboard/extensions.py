"""Flask extensions instantiated once and shared across blueprints."""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager

# Explicit memory:// silences the "no storage backend specified" warning.
# Two-worker gunicorn means counters aren't shared across workers, but
# limits are loose anyway and the league traffic is tiny.
limiter = Limiter(
    key_func=get_remote_address, default_limits=[], storage_uri="memory://"
)
login_manager = LoginManager()
