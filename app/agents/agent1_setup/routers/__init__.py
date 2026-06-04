from app.agents.agent1_setup.routers.users import router as users_router
from app.agents.agent1_setup.routers.channels import router as channels_router
from app.agents.agent1_setup.routers.suggest import router as suggest_router
from app.agents.agent1_setup.routers.voices import router as voices_router

__all__ = ["users_router", "channels_router", "suggest_router", "voices_router"]
