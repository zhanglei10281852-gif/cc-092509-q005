from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import audit, auth, roles, system, users
from app.core.errors import DomainError
from app.database import close_connection, get_connection, init_db, transaction
from app.archives.router import router as archives_router
from app.archives.extended_router import router as archive_operations_router
from app.deadlines.router import router as deadlines_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_db()
    # 重启恢复：未完成节点的提醒排程缺失时幂等补齐；已存在的未来任务不会被重建
    with transaction(immediate=True) as connection:
        from app.deadlines.service import DeadlineService

        DeadlineService(connection).recover_reminders()
    yield
    close_connection()


app = FastAPI(title="专利与技术秘密档案管理服务", version="1.0.0", lifespan=lifespan)


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "context": exc.context}},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(system.router)
app.include_router(archives_router)
app.include_router(archive_operations_router)
app.include_router(deadlines_router)


@app.get("/")
def root() -> dict:
    return {"service": "专利与技术秘密档案管理服务", "version": "1.0.0"}
