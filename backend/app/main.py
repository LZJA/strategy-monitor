from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import accounts, admin, auth, signals
from app.core.config import settings
from app.core.database import Base, engine


Base.metadata.create_all(bind=engine)

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(signals.router, prefix="/api")
app.include_router(accounts.router, prefix="/api")
app.include_router(admin.router, prefix="/api")


@app.get("/api/health")
def health():
    return {"ok": True, "app": settings.app_name}
