"""Demo FastAPI entrypoint combining core Agendo API with ESP32 bench router and hook."""

from __future__ import annotations

from agendo.api import create_app
from agendo.restore import register_post_restore_hook
from demo.hardware_hook import esp32_post_restore_hook
from demo.router import router as demo_router

register_post_restore_hook(esp32_post_restore_hook)

app = create_app()
app.include_router(demo_router)
