"""仅回环访问的本机账号配置入口；setup不执行任何模型推理。"""

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from open_notebook.ai.local_model_service import local_model_status, setup_local_models
from open_notebook.ai.omp import LocalModelError, validate_model_name


def require_loopback(request: Request) -> None:
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(
            status_code=403, detail="本机账号模型配置仅允许回环地址访问。"
        )
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise HTTPException(status_code=403, detail="本机模型配置不接受非本地Host。")
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            allowed = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                and not parsed.username
                and not parsed.password
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise HTTPException(
                status_code=403, detail="本机模型配置不接受外部网页跨站请求。"
            )


router = APIRouter(
    prefix="/local-model",
    tags=["local-model"],
    dependencies=[Depends(require_loopback)],
)


class LocalModelSetup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        return validate_model_name(value) if value is not None else None


@router.get("/status")
async def status():
    return await local_model_status()


@router.post("/setup")
async def setup(body: LocalModelSetup):
    try:
        return await setup_local_models(body.model)
    except LocalModelError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
