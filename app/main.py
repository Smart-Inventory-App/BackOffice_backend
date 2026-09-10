from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import Base, engine

# Register models before create_all
from app.models import admin as _admin  # noqa: F401
from app.models import app_models as _app_models  # noqa: F401
from app.models import audit as _audit  # noqa: F401
from app.api import activity, alerts, auth, me, me_ocr, metrics, ml, ocr, products, stats, users

# Creates `admin_users`. Mirrored tables (auth_users/products/user_profiles)
# already exist (created by their owning services); create_all skips them.
Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.API_TITLE, version=settings.API_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(stats.router)
app.include_router(users.router)
app.include_router(products.router)
app.include_router(me.router)
app.include_router(activity.router)
app.include_router(ml.router)
app.include_router(alerts.router)
app.include_router(metrics.router)
app.include_router(ocr.router)
app.include_router(me_ocr.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "service": "backoffice_backend"}
