from fastapi import FastAPI
from fastapi.middleware import Middleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response, FileResponse
from contextlib import asynccontextmanager

from tortoise import Tortoise
from tortoise.contrib.fastapi import register_tortoise

from app.core.database import init_db
from app.core.exceptions import (
    DoesNotExist,
    DoesNotExistHandle,
    HTTPException,
    HttpExcHandle,
    IntegrityError,
    IntegrityHandle,
    RequestValidationError,
    RequestValidationHandle,
    ResponseValidationError,
    ResponseValidationHandle,
)
from app.core.middlewares import BackGroundTaskMiddleware
from app.utils.logger import logger
from app.utils.config import settings
from app.api import api_router
from app.controllers.user import UserCreate, user_controller
from app.controllers.setting import SettingCreate, setting_controller
from app.controllers.api_token import api_token_controller


class CachedStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path,
        stat_result,
        scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        # 为静态资源添加缓存头
        response.headers["Cache-Control"] = "public, max-age=86400"  # 1天缓存
        return response


class OptimizedImageStaticFiles(CachedStaticFiles):
    def __init__(self, *args, cache_dir: str, **kwargs):
        super().__init__(*args, **kwargs)
        from pathlib import Path

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def get_response(self, path: str, scope):
        from urllib.parse import parse_qs
        from starlette.concurrency import run_in_threadpool

        query_string = (scope.get("query_string") or b"").decode("utf-8", errors="ignore")
        if not query_string:
            return await super().get_response(path, scope)

        qs = parse_qs(query_string)
        if not qs:
            return await super().get_response(path, scope)

        try:
            w = int((qs.get("w") or [0])[0] or 0)
            h = int((qs.get("h") or [0])[0] or 0)
            q = int((qs.get("q") or [0])[0] or 0)
        except Exception:
            return await super().get_response(path, scope)

        auto = ((qs.get("auto") or [None])[0] or "").lower()
        fmt = ((qs.get("format") or [None])[0] or "").lower()
        fit = ((qs.get("fit") or [None])[0] or "").lower()

        wants_resize = (w > 0 or h > 0) and (w <= 5000 and h <= 5000)
        wants_quality = q > 0
        wants_format = bool(fmt) or ("format" in auto)
        if not (wants_resize or wants_quality or wants_format):
            return await super().get_response(path, scope)

        full_path, stat_result = self.lookup_path(path)
        if stat_result is None:
            return Response(status_code=404)

        output_format = fmt
        if not output_format and "format" in auto:
            output_format = "webp"
        if output_format not in {"webp", "jpeg", "jpg", "png"}:
            output_format = "webp" if ("format" in auto) else ""

        q = max(1, min(q or 75, 95))
        w = max(1, min(w or 0, 5000)) if w > 0 else 0
        h = max(1, min(h or 0, 5000)) if h > 0 else 0
        fit = fit if fit in {"cover", "inside", "contain"} else "inside"

        try:
            from hashlib import sha1

            mtime = int(stat_result.st_mtime)
            key = sha1(f"{full_path}:{mtime}:{w}:{h}:{q}:{fit}:{output_format}".encode("utf-8")).hexdigest()
        except Exception:
            return await super().get_response(path, scope)

        ext = ""
        if output_format in {"jpg", "jpeg"}:
            ext = ".jpg"
        elif output_format == "png":
            ext = ".png"
        elif output_format == "webp":
            ext = ".webp"
        cache_path = self.cache_dir / f"{key}{ext}"

        if cache_path.exists():
            resp = FileResponse(cache_path)
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp

        try:
            import os
            from pathlib import Path

            src_path = Path(full_path)
            if not src_path.exists() or not src_path.is_file():
                return Response(status_code=404)

            tmp_path = self.cache_dir / f"{key}.tmp"

            def process_image():
                from PIL import Image, ImageOps

                with Image.open(str(src_path)) as img:
                    img.load()

                    if w and h:
                        if fit == "cover":
                            img = ImageOps.fit(img, (w, h), method=Image.Resampling.LANCZOS)
                        else:
                            img = ImageOps.contain(img, (w, h), method=Image.Resampling.LANCZOS)
                    elif w or h:
                        target_w = w or img.width
                        target_h = h or img.height
                        img = ImageOps.contain(img, (target_w, target_h), method=Image.Resampling.LANCZOS)

                    save_format = output_format.upper() if output_format else (img.format or "JPEG")
                    if save_format == "JPG":
                        save_format = "JPEG"

                    if save_format == "JPEG" and img.mode in {"RGBA", "LA"}:
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                    elif save_format == "JPEG" and img.mode not in {"RGB", "L"}:
                        img = img.convert("RGB")

                    if save_format == "WEBP":
                        img.save(str(tmp_path), format="WEBP", quality=q, method=6)
                    elif save_format == "PNG":
                        img.save(str(tmp_path), format="PNG", optimize=True)
                    else:
                        img.save(str(tmp_path), format="JPEG", quality=q, optimize=True, progressive=True)

                os.replace(str(tmp_path), str(cache_path))

            await run_in_threadpool(process_image)

            resp = FileResponse(cache_path)
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
        except Exception:
            try:
                if cache_path.exists():
                    cache_path.unlink()
            except Exception:
                pass
            return await super().get_response(path, scope)


def register_db(app: FastAPI, db_url=None):
    register_tortoise(
        app,
        config=settings.TORTOISE_ORM,
        generate_schemas=True,
    )


def register_exceptions(app: FastAPI):
    app.add_exception_handler(DoesNotExist, DoesNotExistHandle)
    app.add_exception_handler(HTTPException, HttpExcHandle)
    app.add_exception_handler(IntegrityError, IntegrityHandle)
    app.add_exception_handler(RequestValidationError, RequestValidationHandle)
    app.add_exception_handler(ResponseValidationError, ResponseValidationHandle)


def register_routers(app: FastAPI, prefix: str = "/api"):
    app.include_router(api_router, prefix=prefix)


async def init_superuser():
    user = await user_controller.model.exists()
    if not user:
        await user_controller.create(
            UserCreate(
                username="admin",
                email="admin@admin.com",
                password="123456",
                avatar="https://avatars.githubusercontent.com/u/72618337?v=4",
            )
        )


async def ensure_default_api_tokens():
    users = await user_controller.model.all()
    for user in users:
        try:
            await api_token_controller.create_default_token(user.id)
        except Exception as e:
            logger.error(f"默认Token初始化失败: {str(e)}")


async def init_setting():
    """初始化设置，确保所有字段都有默认值"""
    from migrations.init_default_settings import init_all_default_settings
    
    # 使用完整的默认设置初始化
    try:
        await init_all_default_settings(standalone=False)
        logger.info("设置初始化完成")
    except Exception as e:
        logger.error(f"设置初始化失败: {str(e)}")
        # 如果新的初始化失败，使用原有的简单初始化作为备用
        setting = await setting_controller.model.exists()
        if not setting:
            await setting_controller.create(
                SettingCreate(general={}, content={}, meta={}, storage={}, database={})
            )
            logger.info("使用简单设置初始化完成")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化应用...")
    
    # 首先初始化数据库
    try:
        await init_db()
        logger.info("数据库初始化完成")
    except Exception as e:
        logger.error(f"数据库初始化失败，应用无法启动: {str(e)}")
        raise
    
    # 然后初始化用户和设置
    try:
        await init_superuser()
        await init_setting()
        await ensure_default_api_tokens()
        logger.info("用户和设置初始化完成")
    except Exception as e:
        logger.error(f"用户和设置初始化失败: {str(e)}")
        # 这里不抛出异常，允许应用继续启动

    app.mount(
        "/assets",
        CachedStaticFiles(directory=f"./dist/assets"),
        name="assets",
    )
    
    # 挂载本地存储的图片目录
    import os
    from pathlib import Path
    
    # 获取存储设置中的本地路径
    try:
        setting = await setting_controller.get(id=1)
        local_path = setting.storage.get("local_path", "images")
    except:
        local_path = "images"
    
    # 确保图片目录存在，使用DATA_DIR作为基础路径
    if os.path.isabs(local_path):
        images_dir = Path(local_path)
    else:
        images_dir = Path(settings.DATA_DIR) / local_path
    images_dir.mkdir(parents=True, exist_ok=True)
    
    # 挂载图片静态文件服务
    cache_dir = Path(settings.DATA_DIR) / ".image_cache"
    app.mount(
        f"/{local_path}",
        OptimizedImageStaticFiles(directory=str(images_dir), cache_dir=str(cache_dir)),
        name="images",
    )
    app.state.images_mount_path = f"/{local_path}"

    logger.info("应用初始化完成")

    try:
        yield
    finally:
        logger.info("正在关闭应用...")
        await Tortoise.close_connections()
        logger.info("应用已关闭")


app = FastAPI(
    lifespan=lifespan,
    title=settings.APP_TITLE,
    description=settings.APP_DESCRIPTION,
    version=settings.VERSION,
    openapi_url="/openapi.json",
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
            allow_methods=settings.CORS_ALLOW_METHODS,
            allow_headers=settings.CORS_ALLOW_HEADERS,
        ),
        Middleware(BackGroundTaskMiddleware),
    ],
)

register_db(app)
register_exceptions(app)
register_routers(app, prefix="/api")


@app.get("/")
async def index():
    return HTMLResponse(
        content=open(f"./dist/index.html", "r", encoding="utf-8").read(),
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=300"},
    )

@app.exception_handler(404)
async def not_found_handler(request, exc):
    # 只对非API路径返回前端页面
    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        return JSONResponse(
            content={"code": 404, "msg": "API endpoint not found", "data": None},
            status_code=404,
        )
    if path.startswith("/assets/"):
        return Response(status_code=404)
    images_mount_path = getattr(request.app.state, "images_mount_path", None)
    if images_mount_path and (path == images_mount_path or path.startswith(images_mount_path + "/")):
        return Response(status_code=404)
    if "/." in path:
        return Response(status_code=404)
    last_segment = path.rsplit("/", 1)[-1]
    if "." in last_segment:
        return Response(status_code=404)
    return HTMLResponse(
        content=open(f"./dist/index.html", "r", encoding="utf-8").read(),
        media_type="text/html",
        headers={"Cache-Control": "public, max-age=300"},
    )
