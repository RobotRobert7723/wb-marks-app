from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from wb_marks_app.config import load_config
from wb_marks_app.db import create_all, session_scope
from wb_marks_app.exceptions import AppError, ManualStepRequired
from wb_marks_app.mailer import send_password_reset_email, smtp_configured
from wb_marks_app.server_auth import (
    authenticate_user,
    consume_password_reset,
    get_reset_user,
    register_user,
    request_password_reset,
)
from wb_marks_app.server_models import (
    AppSettingsModel,
    ArtifactModel,
    MarkCodeModel,
    TeksherOperationModel,
    UserModel,
    WorkflowRunItemModel,
    WorkflowRunModel,
)
from wb_marks_app.server_settings import (
    get_or_create_settings,
    settings_public_dict,
    settings_to_app_config,
    update_settings,
)
from wb_marks_app.services.browser import BrowserSessionManager
from wb_marks_app.services.gtin_excel import GtinExcelParser
from wb_marks_app.services.labels import build_manual_labels, labels_to_dicts, make_label_record
from wb_marks_app.services.product_cards import ProductCardTemplateService
from wb_marks_app.services.server_workflow import LaunchRequest, WorkflowRunService
from wb_marks_app.services.teksher import ExistingTeksherProductError, TeksherService
from wb_marks_app.services.teksher_mapping import TeksherMappingService
from wb_marks_app.services.wb import WBService


templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
workflow_service = WorkflowRunService()
product_card_service = ProductCardTemplateService()
gtin_excel_parser = GtinExcelParser()
teksher_mapping_service = TeksherMappingService()
teksher_product_service = TeksherService(BrowserSessionManager(Path.cwd() / ".teksher-product-cards"))


def _raw_settings_dict(settings: AppSettingsModel) -> dict[str, str]:
    return {
        "wb_api_token": settings.wb_api_token,
        "teksher_password": settings.teksher_password,
    }


def create_app() -> FastAPI:
    create_all()
    app = FastAPI(title="WB Marks App", version="1.0.0")
    config = load_config()
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.secret_key or "dev-secret-change-me",
        session_cookie="wb_marks_session",
        same_site="lax",
        https_only=config.app_base_url.startswith("https://"),
        max_age=60 * 60 * 24 * 30,
    )
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.on_event("startup")
    def _startup() -> None:
        create_all()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> dict:
        return {"status": "ready"}

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/runs" if _user_id_from_session(request) else "/login", status_code=302)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if _user_id_from_session(request):
            return RedirectResponse(url="/runs", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            _base_context(request, message="", login=""),
        )

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, login: str = Form(""), password: str = Form("")):
        with session_scope() as session:
            user = authenticate_user(session, login, password)
            if user is None:
                return templates.TemplateResponse(
                    request,
                    "login.html",
                    _base_context(request, message="Неверный логин или пароль.", login=login.strip()),
                    status_code=400,
                )
            _set_session_user(request, user)
        return RedirectResponse(url="/runs", status_code=303)

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request):
        if _user_id_from_session(request):
            return RedirectResponse(url="/runs", status_code=303)
        return templates.TemplateResponse(
            request,
            "register.html",
            _base_context(request, message="", form={"email": "", "login": ""}),
        )

    @app.get("/forgot-password", response_class=HTMLResponse)
    def forgot_password_page(request: Request):
        return templates.TemplateResponse(
            request,
            "forgot_password.html",
            _base_context(request, message="", form={"email": "", "login": ""}),
        )

    @app.post("/forgot-password", response_class=HTMLResponse)
    def forgot_password_submit(request: Request, email: str = Form(""), login: str = Form("")):
        config = load_config()
        if not smtp_configured(config):
            return templates.TemplateResponse(
                request,
                "forgot_password.html",
                _base_context(
                    request,
                    message="SMTP is not configured.",
                    form={"email": email.strip(), "login": login.strip()},
                ),
                status_code=400,
            )

        with session_scope() as session:
            user, raw_token = request_password_reset(session, config, email, login)
            if user is not None and raw_token is not None:
                reset_link = f"{config.app_base_url.rstrip('/')}/reset-password/{raw_token}"
                send_password_reset_email(config, user.email, user.login, reset_link)

        return templates.TemplateResponse(
            request,
            "forgot_password.html",
            _base_context(
                request,
                message="If the email and login exist, the reset link has been sent.",
                form={"email": "", "login": ""},
            ),
        )

    @app.post("/register", response_class=HTMLResponse)
    def register_submit(
        request: Request,
        email: str = Form(""),
        login: str = Form(""),
        password: str = Form(""),
        password_confirm: str = Form(""),
    ):
        try:
            with session_scope() as session:
                user = register_user(session, email, login, password, password_confirm)
                _set_session_user(request, user)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "register.html",
                _base_context(request, message=str(exc), form={"email": email.strip(), "login": login.strip()}),
                status_code=400,
            )
        return RedirectResponse(url="/settings", status_code=303)

    @app.get("/reset-password/{token}", response_class=HTMLResponse)
    def reset_password_page(request: Request, token: str):
        with session_scope() as session:
            user = get_reset_user(session, token)
            login = "" if user is None else user.login
        if user is None:
            return templates.TemplateResponse(
                request,
                "reset_password.html",
                _base_context(
                    request,
                    message="Reset link is invalid or expired.",
                    token=token,
                    login="",
                    invalid=True,
                ),
                status_code=400,
            )
        return templates.TemplateResponse(
            request,
            "reset_password.html",
            _base_context(request, message="", token=token, login=login, invalid=False),
        )

    @app.post("/reset-password/{token}", response_class=HTMLResponse)
    def reset_password_submit(
        request: Request,
        token: str,
        password: str = Form(""),
        password_confirm: str = Form(""),
    ):
        try:
            with session_scope() as session:
                user = consume_password_reset(session, token, password, password_confirm)
                user_id = user.id
                user_login = user.login
        except ValueError as exc:
            with session_scope() as session:
                reset_user = get_reset_user(session, token)
                reset_login = "" if reset_user is None else reset_user.login
            return templates.TemplateResponse(
                request,
                "reset_password.html",
                _base_context(
                    request,
                    message=str(exc),
                    token=token,
                    login=reset_login,
                    invalid=reset_user is None,
                ),
                status_code=400,
            )
        request.session["user_id"] = user_id
        request.session["login"] = user_login
        return RedirectResponse(url="/settings", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            settings = get_or_create_settings(session, user_id)
            return templates.TemplateResponse(
                request,
                "settings.html",
                _base_context(
                    request,
                    settings=settings_public_dict(settings),
                    raw_settings=_raw_settings_dict(settings),
                    message="",
                ),
            )

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings_page(
        request: Request,
        wb_api_token: str = Form(""),
        wb_api_base_url: str = Form("https://supplies-api.wildberries.ru"),
        teksher_username: str = Form(""),
        teksher_password: str = Form(""),
        teksher_transgran_recipient_name: str = Form(""),
        teksher_transgran_recipient_inn: str = Form(""),
        teksher_transgran_recipient_kpp: str = Form(""),
        mapping_mode: str = Form("size"),
        mapping_payload: str = Form("{}"),
        artifact_storage_dir: str = Form(""),
        transgran_document_number_prefix: str = Form("WB"),
        step_timeout_seconds: int = Form(300),
    ):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        payload = {
            "wb_api_token": wb_api_token.strip(),
            "wb_api_base_url": wb_api_base_url.strip(),
            "teksher_username": teksher_username.strip(),
            "teksher_password": teksher_password,
            "teksher_transgran_recipient_name": teksher_transgran_recipient_name.strip(),
            "teksher_transgran_recipient_inn": teksher_transgran_recipient_inn.strip(),
            "teksher_transgran_recipient_kpp": teksher_transgran_recipient_kpp.strip(),
            "mapping_mode": mapping_mode.strip() or "size",
            "mapping_payload": mapping_payload.strip() or "{}",
            "artifact_storage_dir": artifact_storage_dir.strip(),
            "transgran_document_number_prefix": transgran_document_number_prefix.strip() or "WB",
            "step_timeout_seconds": step_timeout_seconds,
        }
        with session_scope() as session:
            settings = update_settings(session, user_id, payload)
            public = settings_public_dict(settings)
            raw_settings = _raw_settings_dict(settings)
        return templates.TemplateResponse(
            request,
            "settings.html",
            _base_context(request, settings=public, raw_settings=raw_settings, message="Settings saved."),
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            runs = session.execute(
                select(WorkflowRunModel)
                .where(WorkflowRunModel.user_id == user_id)
                .order_by(WorkflowRunModel.created_at.desc())
            ).scalars().all()
            return templates.TemplateResponse(request, "runs.html", _base_context(request, runs=runs))

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail_page(request: Request, run_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            run = _get_user_run(session, run_id, user_id)
            items = session.execute(
                select(WorkflowRunItemModel)
                .where(WorkflowRunItemModel.run_id == run_id)
                .order_by(WorkflowRunItemModel.vendor_code, WorkflowRunItemModel.size)
            ).scalars().all()
            operations = session.execute(
                select(TeksherOperationModel).join(WorkflowRunItemModel)
                .where(WorkflowRunItemModel.run_id == run_id)
                .order_by(WorkflowRunItemModel.vendor_code, WorkflowRunItemModel.size, TeksherOperationModel.operation_kind)
            ).scalars().all()
            artifacts = session.execute(
                select(ArtifactModel).join(WorkflowRunItemModel).where(WorkflowRunItemModel.run_id == run_id)
            ).scalars().all()
            return templates.TemplateResponse(
                request,
                "run_detail.html",
                _base_context(
                    request,
                    run=run,
                    items=items,
                    operations=operations,
                    artifacts={artifact.run_item_id: artifact for artifact in artifacts},
                ),
            )

    @app.get("/runs/{run_id}/labels", response_class=HTMLResponse)
    def run_labels_page(request: Request, run_id: str, template: str = "combined"):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            run = _get_user_run(session, run_id, user_id)
            items = session.execute(
                select(WorkflowRunItemModel)
                .where(WorkflowRunItemModel.run_id == run_id)
                .order_by(WorkflowRunItemModel.vendor_code, WorkflowRunItemModel.size)
            ).scalars().all()
            labels = []
            for item in items:
                marks = session.execute(
                    select(MarkCodeModel)
                    .where(MarkCodeModel.run_item_id == item.id)
                    .order_by(MarkCodeModel.position)
                ).scalars().all()
                for mark in marks:
                    labels.append(
                        make_label_record(
                            template=template,
                            item_name=item.wb_item_name,
                            vendor_code=item.vendor_code,
                            size=item.size,
                            wb_barcode=item.barcode,
                            mark_code=mark.mark_code,
                            index=mark.position,
                            total=item.quantity,
                        )
                    )
            return templates.TemplateResponse(
                request,
                "labels_print.html",
                _base_context(
                    request,
                    title=f"Labels for WB draft {run.draft_id}",
                    labels=labels_to_dicts(labels),
                    label_type=template,
                    message="" if labels else "В этом запуске пока нет сохраненных кодов ЧЗ.",
                ),
            )

    @app.get("/labels", response_class=HTMLResponse)
    def labels_page(request: Request):
        if not _user_id_from_session(request):
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "labels.html",
            _base_context(request, message="", form=_default_label_form()),
        )

    @app.get("/product-cards/{wb_article}", response_class=HTMLResponse)
    def product_card_page(request: Request, wb_article: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            settings = get_or_create_settings(session, user_id)
            config = settings_to_app_config(settings)
        product_card = product_card_service.build_template(wb_article, config)
        with session_scope() as session:
            mapping_version, mapping_rows = teksher_mapping_service.latest_payload(session, user_id, product_card.wb_article)
        teksher_rows_by_gtin: dict[str, dict] | None = None
        teksher_status = ""
        if mapping_rows:
            try:
                teksher_rows_by_gtin = teksher_product_service.product_mapping_rows_by_gtins(
                    [row.get("gtin", "") for row in mapping_rows],
                    config,
                )
            except (AppError, ManualStepRequired) as exc:
                teksher_rows_by_gtin = {}
                teksher_status = f" Данные Текшер по GTIN не загружены: {exc}"
        product_card = teksher_mapping_service.apply_payload(
            product_card,
            mapping_rows,
            mapping_version,
            teksher_rows_by_gtin,
        )
        if teksher_status:
            product_card = replace(product_card, api_status=(product_card.api_status + teksher_status).strip())
        return templates.TemplateResponse(
            request,
            "product_card.html",
            _base_context(request, product_card=product_card),
        )

    @app.post("/api/product-cards/{wb_article}/gtin-upload")
    async def upload_product_card_gtin(request: Request, wb_article: str, file: UploadFile = File(...)):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        if not _is_excel_file(file.filename or ""):
            return JSONResponse({"ok": False, "message": "Загрузите Excel файл формата .xlsx или .xlsm.", "rows": []})

        content = await file.read()
        try:
            gtin_rows = gtin_excel_parser.parse(content)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc), "rows": []})

        with session_scope() as session:
            settings = get_or_create_settings(session, user_id)
            config = settings_to_app_config(settings)
        product_card = product_card_service.build_template(wb_article, config)
        seller_article = product_card.wb_summary.seller_article.strip()
        if not seller_article:
            return JSONResponse(
                {
                    "ok": False,
                    "message": product_card.api_status or "Артикул продавца WB не загружен.",
                    "rows": [],
                }
            )
        matched_rows = gtin_excel_parser.find_by_vendor_article(gtin_rows, seller_article)
        if not matched_rows:
            return JSONResponse({"ok": False, "message": "Артикул продавца в файле не найден", "rows": []})

        return {
            "ok": True,
            "message": f"GTIN загружены: {len(matched_rows)} строк.",
            "seller_article": seller_article,
            "rows": [_serialize_gtin_row(row) for row in matched_rows],
        }

    @app.post("/api/product-cards/{wb_article}/mapping")
    async def save_product_card_mapping(request: Request, wb_article: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "message": "Некорректный JSON."}, status_code=400)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return JSONResponse({"ok": False, "message": "Нет строк мэппинга для сохранения."}, status_code=400)

        try:
            with session_scope() as session:
                settings = get_or_create_settings(session, user_id)
                config = settings_to_app_config(settings)
            product_card = product_card_service.build_template(wb_article, config)
            draft_ids = teksher_product_service.ensure_product_drafts_for_mapping(product_card, rows, config)
            with session_scope() as session:
                version, created = teksher_mapping_service.save_version(
                    session,
                    user_id,
                    product_card,
                    rows,
                    source=str(payload.get("source") or "product_card"),
                )
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        except ExistingTeksherProductError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=409)
        except (AppError, ManualStepRequired) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

        return {
            "ok": True,
            "message": f"Мэппинг сохранен: версия {version}, строк {created}.",
            "version": version,
            "rows_saved": created,
            "teksher_draft_ids": draft_ids,
        }

    @app.post("/labels/preview", response_class=HTMLResponse)
    def labels_preview_page(
        request: Request,
        template: str = Form("combined"),
        item_name: str = Form(""),
        vendor_code: str = Form(""),
        size: str = Form(""),
        color: str = Form(""),
        composition: str = Form(""),
        wb_barcode: str = Form(""),
        mark_codes: str = Form(""),
        unit_count: str = Form("1"),
        copies: int = Form(1),
        note_text: str = Form(""),
    ):
        if not _user_id_from_session(request):
            return RedirectResponse(url="/login", status_code=303)
        labels = build_manual_labels(
            template=template,
            item_name=item_name,
            vendor_code=vendor_code,
            size=size,
            color=color,
            composition=composition,
            wb_barcode=wb_barcode,
            mark_codes_text=mark_codes,
            unit_count=unit_count,
            copies=copies,
            note_text=note_text,
        )
        if not labels:
            return templates.TemplateResponse(
                request,
                "labels.html",
                _base_context(
                    request,
                    message="Нет данных для печати.",
                    form={
                        "template": template,
                        "item_name": item_name,
                        "vendor_code": vendor_code,
                        "size": size,
                        "color": color,
                        "composition": composition,
                        "wb_barcode": wb_barcode,
                        "mark_codes": mark_codes,
                        "unit_count": unit_count,
                        "copies": copies,
                        "note_text": note_text,
                    },
                ),
            )
        return templates.TemplateResponse(
            request,
            "labels_print.html",
            _base_context(
                request,
                title="Этикетки 60x40",
                labels=labels_to_dicts(labels),
                label_type=template,
                message="",
            ),
        )

    @app.post("/runs/{run_id}/resume")
    def resume_run(request: Request, run_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            _get_user_run(session, run_id, user_id)
        workflow_service.start_background(run_id)
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/retry-failed")
    def retry_failed(request: Request, run_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            _get_user_run(session, run_id, user_id)
        workflow_service.retry_failed_items(run_id)
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @app.post("/runs/{run_id}/cancel")
    def cancel_run(request: Request, run_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            _get_user_run(session, run_id, user_id)
        workflow_service.cancel_run(run_id)
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @app.post("/api/launches")
    async def create_launch(request: Request):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        payload = await request.json()
        draft_id = str(payload.get("draft_id") or "").strip()
        if not draft_id:
            raise HTTPException(status_code=400, detail="draft_id is required")
        with session_scope() as session:
            settings = get_or_create_settings(session, user_id)
            if _settings_incomplete(settings):
                return JSONResponse(
                    {
                        "settings_required": True,
                        "settings_url": "/settings",
                    },
                    status_code=409,
                )
        run_id = workflow_service.create_or_resume_run(
            LaunchRequest(user_id=user_id, draft_id=draft_id, source_url=str(payload.get("source_url") or ""))
        )
        return {
            "launch_id": run_id,
            "status_url": f"/runs/{run_id}",
            "settings_required": False,
        }

    @app.get("/api/launches/{run_id}")
    def get_launch(request: Request, run_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        with session_scope() as session:
            run = _get_user_run(session, run_id, user_id)
            return _serialize_run(run)

    @app.get("/api/launches/{run_id}/items")
    def get_launch_items(request: Request, run_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        with session_scope() as session:
            _get_user_run(session, run_id, user_id)
            items = session.execute(
                select(WorkflowRunItemModel)
                .where(WorkflowRunItemModel.run_id == run_id)
                .order_by(WorkflowRunItemModel.vendor_code, WorkflowRunItemModel.size)
            ).scalars().all()
            return [_serialize_item(item, session) for item in items]

    @app.get("/api/settings")
    def get_settings(request: Request):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        with session_scope() as session:
            settings = get_or_create_settings(session, user_id)
            return settings_public_dict(settings)

    @app.put("/api/settings")
    async def put_settings(request: Request):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        payload = await request.json()
        with session_scope() as session:
            settings = update_settings(session, user_id, payload)
            return settings_public_dict(settings)

    @app.post("/api/settings/validate")
    async def validate_settings(request: Request):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        payload = await request.json()
        with session_scope() as session:
            settings = get_or_create_settings(session, user_id)
            merged = settings_public_dict(settings)
            merged.update(payload)
            merged["wb_api_token"] = payload.get("wb_api_token", settings.wb_api_token)
            merged["teksher_password"] = payload.get("teksher_password", settings.teksher_password)
            merged["teksher_username"] = payload.get("teksher_username", settings.teksher_username)
            merged["teksher_transgran_recipient_name"] = payload.get(
                "teksher_transgran_recipient_name",
                settings.teksher_transgran_recipient_name,
            )
            merged["teksher_transgran_recipient_inn"] = payload.get(
                "teksher_transgran_recipient_inn",
                settings.teksher_transgran_recipient_inn,
            )
            merged["teksher_transgran_recipient_kpp"] = payload.get(
                "teksher_transgran_recipient_kpp",
                settings.teksher_transgran_recipient_kpp,
            )
            config = settings_to_app_config(settings)
            config.wb_api_token = merged["wb_api_token"]
            config.wb_api_base_url = merged.get("wb_api_base_url", settings.wb_api_base_url)
            config.teksher_username = merged["teksher_username"]
            config.teksher_password = merged["teksher_password"]
            browser = BrowserSessionManager(Path.cwd() / ".noop")
            wb_service = WBService(browser)
            teksher_service = TeksherService(browser)
            wb_service.validate_connection(config)
            teksher_service.validate_connection(config)
            return {"ok": True}

    @app.get("/api/artifacts/{artifact_id}")
    def get_artifact(request: Request, artifact_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        with session_scope() as session:
            artifact = session.get(ArtifactModel, artifact_id)
            if artifact is None:
                raise HTTPException(status_code=404, detail="Artifact not found")
            item = session.get(WorkflowRunItemModel, artifact.run_item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="Artifact item not found")
            run = session.get(WorkflowRunModel, item.run_id)
            if run is None or run.user_id != user_id:
                raise HTTPException(status_code=404, detail="Artifact not found")
            return FileResponse(path=artifact.file_path, filename=artifact.file_name, media_type="text/csv")

    return app


def _serialize_run(run: WorkflowRunModel) -> dict:
    try:
        summary = json.loads(run.summary_json or "{}")
    except json.JSONDecodeError:
        summary = {}
    return {
        "id": run.id,
        "draft_id": run.draft_id,
        "status": run.status,
        "error": run.error,
        "source_url": run.source_url,
        "summary": summary,
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "updated_at": run.updated_at.isoformat() if run.updated_at else "",
    }


def _serialize_item(item: WorkflowRunItemModel, session: Session) -> dict:
    artifact = session.execute(
        select(ArtifactModel)
        .where(ArtifactModel.run_item_id == item.id)
        .where(ArtifactModel.kind == "csv")
    ).scalars().first()
    operations = session.execute(
        select(TeksherOperationModel).where(TeksherOperationModel.run_item_id == item.id)
    ).scalars().all()
    return {
        "id": item.id,
        "barcode": item.barcode,
        "vendor_code": item.vendor_code,
        "size": item.size,
        "gtin": item.gtin,
        "quantity": item.quantity,
        "status": item.status,
        "error": item.error,
        "document_number": item.document_number,
        "artifact_id": artifact.id if artifact else None,
        "artifact_name": artifact.file_name if artifact else None,
        "operations": [
            {
                "kind": op.operation_kind,
                "external_operation_id": op.external_operation_id,
                "status": op.status,
                "end_at": op.end_at,
            }
            for op in operations
        ],
    }


def _settings_incomplete(settings: AppSettingsModel) -> bool:
    return not all(
        [
            settings.wb_api_token.strip(),
            settings.teksher_username.strip(),
            settings.teksher_password.strip(),
            settings.teksher_transgran_recipient_name.strip(),
            settings.teksher_transgran_recipient_inn.strip(),
            settings.teksher_transgran_recipient_kpp.strip(),
            settings.mapping_payload.strip(),
        ]
    )


def _default_label_form() -> dict:
    return {
        "template": "combined",
        "item_name": "",
        "vendor_code": "",
        "size": "",
        "color": "",
        "composition": "",
        "wb_barcode": "",
        "mark_codes": "",
        "unit_count": "1",
        "copies": 1,
        "note_text": "",
    }


def _is_excel_file(filename: str) -> bool:
    return filename.lower().endswith((".xlsx", ".xlsm"))


def _serialize_gtin_row(row) -> dict:
    return {
        "gtin": row.gtin,
        "brand": row.brand,
        "functional_name": row.functional_name,
        "variety": row.variety,
        "vendor_article": row.vendor_article,
        "color": row.color,
        "size": row.size,
    }


def _base_context(request: Request, **extra) -> dict:
    context = {
        "request": request,
        "current_user": {
            "id": request.session.get("user_id", ""),
            "login": request.session.get("login", ""),
        },
    }
    context.update(extra)
    return context


def _user_id_from_session(request: Request) -> str:
    return str(request.session.get("user_id") or "").strip()


def _set_session_user(request: Request, user: UserModel) -> None:
    request.session["user_id"] = user.id
    request.session["login"] = user.login


def _get_user_run(session: Session, run_id: str, user_id: str) -> WorkflowRunModel:
    run = session.execute(
        select(WorkflowRunModel)
        .where(WorkflowRunModel.id == run_id)
        .where(WorkflowRunModel.user_id == user_id)
    ).scalars().first()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
