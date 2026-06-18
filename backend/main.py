from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.routes.upload import router as upload_router
from backend.routes.agent import router as agent_router
from backend.routes.sources import router as sources_router
from backend.routes.insights import router as insights_router

app = FastAPI(title="METI Lab API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload_router, prefix="/api")
app.include_router(agent_router, prefix="/api")
app.include_router(sources_router, prefix="/api")
app.include_router(insights_router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok", "service": "meti-lab-backend"}
