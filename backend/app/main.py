"""Point d'entrée FastAPI."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth, delivery_points, drifts, exports, organizations, partner, regulatory
from app.config import settings
from app.repositories import ResourceNotFound
from app.services.consent import ConsentRequiredError
from app.services.onboarding import ConflictError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler = None
    if settings.enable_scheduler:
        from app.scheduler import start_scheduler

        scheduler = start_scheduler()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=False)


app = FastAPI(title="EffiSmart API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ResourceNotFound)
async def _not_found(_: Request, __: ResourceNotFound) -> JSONResponse:
    # Une ressource hors périmètre est indiscernable d'une ressource inexistante.
    return JSONResponse({"detail": "Ressource introuvable"}, status_code=status.HTTP_404_NOT_FOUND)


@app.exception_handler(ConflictError)
async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)


@app.exception_handler(ConsentRequiredError)
async def _consent_required(_: Request, __: ConsentRequiredError) -> JSONResponse:
    return JSONResponse(
        {"detail": "Consentement actif requis pour accéder aux données de ce point de livraison"},
        status_code=status.HTTP_403_FORBIDDEN,
    )


for module in (auth, organizations, delivery_points, drifts, regulatory, exports, partner):
    app.include_router(module.router, prefix="/api")


@app.get("/api/health", tags=["système"])
def health() -> dict:
    return {"status": "ok"}
