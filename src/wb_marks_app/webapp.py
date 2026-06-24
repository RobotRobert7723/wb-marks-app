from __future__ import annotations

import json
import hashlib
import re
import secrets
import uuid
from dataclasses import replace
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
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
    LabelApiJobModel,
    LabelApiJobRowModel,
    LabelPrintJobModel,
    MarkCodeModel,
    TeksherOperationModel,
    UserModel,
    WorkflowRunItemModel,
    WorkflowRunModel,
)
from wb_marks_app.server_settings import (
    get_settings_by_store_id,
    get_or_create_settings,
    settings_public_dict,
    settings_to_app_config,
    update_settings,
)
from wb_marks_app.services.browser import BrowserSessionManager
from wb_marks_app.services.gtin_excel import GtinExcelParser
from wb_marks_app.services.label_pdf import LabelPdfError, render_labels_pdf
from wb_marks_app.services.labels import build_manual_labels, extract_gs1_mark_codes, labels_to_dicts, make_label_record
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
_LABEL_PDF_ID_RE = re.compile(r"^[a-f0-9]{32}$")


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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://seller.wildberries.ru"],
        allow_origin_regex=r"^chrome-extension://.+$",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
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

    @app.post("/api/v1/labels/readiness")
    async def label_api_readiness(request: Request):
        auth_error = _label_api_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            payload = await _label_api_json_payload(request)
        except ValueError as exc:
            return _label_api_error(str(exc), 400)

        wb_store_id = _label_api_text(payload.get("wbStoreId"))
        nm_id = _label_api_text(payload.get("nmId"))
        sizes = _label_api_sizes(payload.get("sizes"))
        settings_url = _label_api_settings_url(nm_id, wb_store_id)

        if not wb_store_id or not nm_id or not sizes:
            return _label_api_error("wbStoreId, nmId and sizes are required.", 400, settingsUrl=settings_url)

        with session_scope() as session:
            user, settings = _label_api_store_context(session, wb_store_id)
            if user is None or settings is None:
                return _label_api_setup_required(nm_id, wb_store_id, "Не найден магазин WB в WB Marks App.")
            if _label_api_settings_incomplete(settings):
                return _label_api_setup_required(nm_id, wb_store_id, "Не заполнены настройки WB Marks App или Текшер.")

            missing = _label_api_missing_mapping_sizes(session, user.id, nm_id, sizes)
            if missing:
                return _label_api_setup_required(
                    nm_id,
                    wb_store_id,
                    f"Не найден мэппинг для размеров: {', '.join(missing)}",
                )

        return {"status": "ready", "message": "Сервис готов к печати"}

    @app.post("/api/v1/labels/print-jobs")
    async def create_label_api_print_job(request: Request):
        auth_error = _label_api_auth_error(request)
        if auth_error is not None:
            return auth_error
        try:
            payload = await _label_api_json_payload(request)
            normalized = _normalize_label_api_print_payload(payload)
        except ValueError as exc:
            return _label_api_error(str(exc), 422)

        request_hash = _label_api_request_hash(normalized)
        wb_store_id = normalized["wbStoreId"]
        nm_id = normalized["nmId"]
        settings_url = _label_api_settings_url(nm_id, wb_store_id)

        with session_scope() as session:
            existing = _label_api_existing_job(session, wb_store_id, normalized["requestId"])
            if existing is not None:
                if existing.request_hash != request_hash:
                    return _label_api_error("requestId уже использован с другими параметрами", 409)
                _refresh_label_api_job(request, session, existing)
                return JSONResponse(_serialize_label_api_job(request, existing), status_code=200)

            user, settings = _label_api_store_context(session, wb_store_id)
            if user is None or settings is None:
                return _label_api_error("Не найден магазин WB в WB Marks App.", 422, settingsUrl=settings_url)
            if _label_api_settings_incomplete(settings):
                return _label_api_error("Не заполнены настройки WB Marks App или Текшер.", 422, settingsUrl=settings_url)

            config = settings_to_app_config(settings)
            version, mapping_rows = teksher_mapping_service.latest_payload(session, user.id, nm_id)
            if version == 0 or not mapping_rows:
                return _label_api_error("Не найден мэппинг для карточки WB.", 422, settingsUrl=settings_url)
            workflow_rows = _label_api_workflow_rows(mapping_rows, normalized["items"])
            missing = _label_api_missing_item_sizes(workflow_rows, normalized["items"])
            if missing:
                return _label_api_error(
                    f"Не найден мэппинг для размеров: {', '.join(missing)}",
                    422,
                    settingsUrl=settings_url,
                )
            user_id = user.id

        try:
            product_card = product_card_service.build_template(nm_id, config)
            run_id = workflow_service.create_product_card_run(user_id, product_card.wb_article, product_card, workflow_rows)
        except ValueError as exc:
            return _label_api_error(str(exc), 422, settingsUrl=settings_url)
        except (AppError, ManualStepRequired) as exc:
            return _label_api_error(str(exc), 400, settingsUrl=settings_url)

        with session_scope() as session:
            job = LabelApiJobModel(
                request_id=normalized["requestId"],
                request_hash=request_hash,
                wb_store_id=wb_store_id,
                store_name=normalized["storeName"],
                user_id=user_id,
                nm_id=product_card.wb_article,
                vendor_code=normalized["vendorCode"],
                template=normalized["template"],
                status="queued",
                run_id=run_id,
            )
            session.add(job)
            session.flush()
            for row in workflow_rows:
                session.add(
                    LabelApiJobRowModel(
                        job_id=job.id,
                        size=row["size"],
                        quantity=row["quantity"],
                        gtin=row["gtin"],
                        status="emission",
                    )
                )
            session.flush()
            response = _serialize_label_api_job(request, job)
        return JSONResponse(response, status_code=202)

    @app.get("/api/v1/labels/print-jobs/{job_id}")
    def get_label_api_print_job(request: Request, job_id: str):
        auth_error = _label_api_auth_error(request)
        if auth_error is not None:
            return auth_error
        with session_scope() as session:
            job = session.get(LabelApiJobModel, job_id)
            if job is None:
                return _label_api_error("Print job not found.", 404)
            _refresh_label_api_job(request, session, job)
            return _serialize_label_api_job(request, job)

    @app.get("/api/v1/labels/files/{file_id}.pdf", name="download_label_api_pdf")
    def download_label_api_pdf(request: Request, file_id: str):
        auth_error = _label_api_auth_error(request)
        if auth_error is not None:
            return auth_error
        if not _LABEL_PDF_ID_RE.fullmatch(file_id):
            raise HTTPException(status_code=404, detail="PDF not found")
        with session_scope() as session:
            job = session.execute(
                select(LabelApiJobModel).where(LabelApiJobModel.pdf_file_id == file_id)
            ).scalars().first()
            if job is None or not job.user_id:
                raise HTTPException(status_code=404, detail="PDF not found")
            file_path = _label_pdf_file_path(job.user_id, file_id)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="PDF not found")
        return FileResponse(
            path=file_path,
            filename=f"labels_{file_id[:8]}_58x40.pdf",
            media_type="application/pdf",
        )

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
        wb_store_id: str = Form(""),
        wb_api_token: str = Form(""),
        wb_api_base_url: str = Form("https://supplies-api.wildberries.ru"),
        teksher_username: str = Form(""),
        teksher_password: str = Form(""),
        teksher_transgran_recipient_name: str = Form(""),
        teksher_transgran_recipient_inn: str = Form(""),
        teksher_transgran_recipient_kpp: str = Form(""),
        supplier_name: str = Form(""),
        production_address: str = Form(""),
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
            "wb_store_id": wb_store_id.strip(),
            "wb_api_token": wb_api_token.strip(),
            "wb_api_base_url": wb_api_base_url.strip(),
            "teksher_username": teksher_username.strip(),
            "teksher_password": teksher_password,
            "teksher_transgran_recipient_name": teksher_transgran_recipient_name.strip(),
            "teksher_transgran_recipient_inn": teksher_transgran_recipient_inn.strip(),
            "teksher_transgran_recipient_kpp": teksher_transgran_recipient_kpp.strip(),
            "supplier_name": supplier_name.strip(),
            "production_address": production_address.strip(),
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
    def run_labels_page(request: Request, run_id: str, template: str = "srad"):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            run, labels = _build_run_labels(session, run_id, user_id, template)
            return templates.TemplateResponse(
                request,
                "labels_print.html",
                _base_context(
                    request,
                    title=f"Labels for WB draft {run.draft_id}",
                    labels=labels_to_dicts(labels),
                    label_type=template,
                    pdf_url=f"/runs/{run_id}/labels.pdf?template={template}",
                    message="" if labels else "В этом запуске пока нет сохраненных кодов ЧЗ.",
                ),
            )

    @app.get("/runs/{run_id}/labels.pdf")
    def run_labels_pdf(request: Request, run_id: str, template: str = "srad"):
        user_id = _user_id_from_session(request)
        if not user_id:
            return RedirectResponse(url="/login", status_code=303)
        with session_scope() as session:
            run, labels = _build_run_labels(session, run_id, user_id, template)
        if not labels:
            raise HTTPException(status_code=404, detail="No labels to print")
        try:
            pdf = render_labels_pdf(labels, template=template)
        except LabelPdfError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        filename = f"wb_labels_{run.draft_id}_58x40.pdf"
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
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
        with session_scope() as session:
            ready_to_print_counts = _product_card_ready_to_print_counts(session, user_id, product_card.wb_article)
        product_card = _apply_product_card_ready_to_print_counts(product_card, ready_to_print_counts)
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

    @app.post("/api/product-cards/{wb_article}/mapping/preview")
    async def preview_product_card_mapping(request: Request, wb_article: str):
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
            preview = teksher_product_service.product_draft_preview_for_mapping(product_card, rows, config)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        except (AppError, ManualStepRequired) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

        return {
            "ok": True,
            "existing_gtins": preview.get("existing_gtins", []),
            "create_gtins": preview.get("create_gtins", []),
            "draft_fields": preview.get("draft_fields", {}),
            "dictionaries": preview.get("dictionaries", {}),
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
        raw_draft_fields = payload.get("draft_fields") if isinstance(payload, dict) else None
        draft_fields = raw_draft_fields if isinstance(raw_draft_fields, dict) else {}

        try:
            with session_scope() as session:
                settings = get_or_create_settings(session, user_id)
                config = settings_to_app_config(settings)
            product_card = product_card_service.build_template(wb_article, config)
            rows_to_save = teksher_product_service.rows_with_product_draft_fields(rows, draft_fields)
            draft_result = teksher_product_service.ensure_product_drafts_result_for_mapping(product_card, rows_to_save, config)
            draft_ids = draft_result.get("draft_ids", [])
            created_gtins = draft_result.get("created_gtins", [])
            existing_gtins = draft_result.get("existing_gtins", [])
            with session_scope() as session:
                version, created = teksher_mapping_service.save_version(
                    session,
                    user_id,
                    product_card,
                    rows_to_save,
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
            "message": _product_mapping_save_message(version, created, created_gtins),
            "version": version,
            "rows_saved": created,
            "teksher_draft_ids": draft_ids,
            "created_gtins": created_gtins,
            "existing_gtins": existing_gtins,
        }

    @app.post("/api/product-cards/{wb_article}/mark-orders")
    async def create_product_card_mark_order(request: Request, wb_article: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"ok": False, "message": "Некорректный JSON."}, status_code=400)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return JSONResponse({"ok": False, "message": "Нет строк для заказа ЧЗ."}, status_code=400)

        try:
            with session_scope() as session:
                settings = get_or_create_settings(session, user_id)
                config = settings_to_app_config(settings)
            product_card = product_card_service.build_template(wb_article, config)
            run_id = workflow_service.create_product_card_run(user_id, product_card.wb_article, product_card, rows)
            with session_scope() as session:
                run = _get_user_run(session, run_id, user_id)
                status_payload = _serialize_product_card_order_run(run, session)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        except (AppError, ManualStepRequired) as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

        return {
            "ok": True,
            "message": "Заказ ЧЗ в Текшер запущен.",
            "run_id": run_id,
            "status": status_payload,
        }

    @app.get("/api/product-cards/{wb_article}/mark-orders")
    def list_product_card_mark_orders(request: Request, wb_article: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        with session_scope() as session:
            return {
                "ok": True,
                "history": _product_card_order_history(session, user_id, wb_article),
            }

    @app.get("/api/product-cards/{wb_article}/mark-orders/{run_id}")
    def get_product_card_mark_order(request: Request, wb_article: str, run_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        with session_scope() as session:
            run = _get_user_run(session, run_id, user_id)
            return {
                "ok": True,
                "status": _serialize_product_card_order_run(run, session),
            }

    @app.post("/api/product-cards/{wb_article}/label-print")
    async def create_product_card_label_print(request: Request, wb_article: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            template = _product_card_label_template(str(payload.get("template") or payload.get("template_name") or "srad"))
            with session_scope() as session:
                settings = get_or_create_settings(session, user_id)
                config = settings_to_app_config(settings)
            product_card = product_card_service.build_template(wb_article, config)
            with session_scope() as session:
                result = _create_product_card_label_pdf(request, session, user_id, product_card, template)
        except ValueError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        except LabelPdfError as exc:
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return result

    @app.get("/api/product-cards/{wb_article}/label-prints")
    def list_product_card_label_prints(request: Request, wb_article: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        with session_scope() as session:
            return {
                "ok": True,
                "history": _product_card_label_print_history(request, session, user_id, wb_article),
            }

    @app.post("/labels/preview", response_class=HTMLResponse)
    def labels_preview_page(
        request: Request,
        template: str = Form("srad"),
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
        supplier_name: str = Form(""),
        production_date: str = Form(""),
        country_of_origin: str = Form(""),
        brand: str = Form(""),
        supplier_address: str = Form(""),
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
            supplier_name=supplier_name,
            production_date=production_date,
            country_of_origin=country_of_origin,
            brand=brand,
            supplier_address=supplier_address,
        )
        if not labels:
            return templates.TemplateResponse(
                request,
                "labels.html",
                _base_context(
                    request,
                    message="Нет данных для печати.",
                    form=_label_form_from_inputs(
                        template=template,
                        item_name=item_name,
                        vendor_code=vendor_code,
                        size=size,
                        color=color,
                        composition=composition,
                        wb_barcode=wb_barcode,
                        mark_codes=mark_codes,
                        unit_count=unit_count,
                        copies=copies,
                        note_text=note_text,
                        supplier_name=supplier_name,
                        production_date=production_date,
                        country_of_origin=country_of_origin,
                        brand=brand,
                        supplier_address=supplier_address,
                    ),
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
                pdf_url="",
                message="",
            ),
        )

    @app.post("/labels/pdf")
    def labels_pdf_page(
        request: Request,
        template: str = Form("srad"),
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
        supplier_name: str = Form(""),
        production_date: str = Form(""),
        country_of_origin: str = Form(""),
        brand: str = Form(""),
        supplier_address: str = Form(""),
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
            supplier_name=supplier_name,
            production_date=production_date,
            country_of_origin=country_of_origin,
            brand=brand,
            supplier_address=supplier_address,
        )
        try:
            pdf = render_labels_pdf(labels, template=template)
        except (LabelPdfError, ValueError) as exc:
            return templates.TemplateResponse(
                request,
                "labels.html",
                _base_context(
                    request,
                    message=str(exc),
                    form=_label_form_from_inputs(
                        template=template,
                        item_name=item_name,
                        vendor_code=vendor_code,
                        size=size,
                        color=color,
                        composition=composition,
                        wb_barcode=wb_barcode,
                        mark_codes=mark_codes,
                        unit_count=unit_count,
                        copies=copies,
                        note_text=note_text,
                        supplier_name=supplier_name,
                        production_date=production_date,
                        country_of_origin=country_of_origin,
                        brand=brand,
                        supplier_address=supplier_address,
                    ),
                ),
                status_code=400,
            )
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="labels_58x40.pdf"'},
        )

    @app.post("/api/labels/pdf")
    async def create_label_pdf_api(request: Request):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        try:
            payload = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="JSON body is required") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")

        template = _payload_text(payload, "template", "template_name", "название_шаблона", "Название шаблона") or "srad"
        mark_codes = _payload_mark_codes(payload)
        if _template_requires_mark_codes(template) and not mark_codes:
            raise HTTPException(status_code=400, detail="mark_codes must contain at least one ЧЗ code")

        labels = build_manual_labels(
            template=template,
            item_name=_payload_text(payload, "item_name", "name", "наименование", "Наименование"),
            vendor_code=_payload_text(payload, "vendor_code", "article", "артикул", "Артикул"),
            size=_payload_text(payload, "size", "размер", "Размер"),
            color=_payload_text(payload, "color", "цвет", "Цвет"),
            composition=_payload_text(payload, "composition", "состав", "Состав"),
            wb_barcode=_payload_text(payload, "wb_barcode", "WB barcode", "wb barcode"),
            mark_codes_text="\n".join(mark_codes),
            unit_count="1",
            copies=max(len(mark_codes), 1),
            note_text=_payload_text(payload, "note_text", "completeness", "комплектность", "Комплектность"),
            supplier_name=_payload_text(payload, "supplier_name", "supplier", "поставщик", "Поставщик"),
            production_date=_payload_text(
                payload,
                "production_date",
                "manufacture_date",
                "дата_производства",
                "Дата производства",
            ),
            country_of_origin=_payload_text(
                payload,
                "country_of_origin",
                "country",
                "страна_производства",
                "Страна производства",
            ),
            brand=_payload_text(payload, "brand", "бренд", "Бренд"),
            supplier_address=_payload_text(
                payload,
                "supplier_address",
                "address",
                "адрес_поставщика",
                "Адрес поставщика",
            ),
        )
        try:
            pdf = render_labels_pdf(labels, template=template)
        except (LabelPdfError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        file_id = uuid.uuid4().hex
        file_path = _label_pdf_file_path(user_id, file_id)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(pdf)
        download_url = str(request.url_for("download_label_pdf_api", file_id=file_id))
        return {
            "ok": True,
            "template": labels[0].template if labels else template,
            "download_url": download_url,
            "file_name": f"labels_{file_id[:8]}_58x40.pdf",
            "labels_count": len(labels),
            "pages_count": _pdf_page_count(pdf),
        }

    @app.get("/api/labels/pdf/{file_id}", name="download_label_pdf_api")
    def download_label_pdf_api(request: Request, file_id: str):
        user_id = _user_id_from_session(request)
        if not user_id:
            return JSONResponse({"login_required": True, "login_url": "/login"}, status_code=401)
        if not _LABEL_PDF_ID_RE.fullmatch(file_id):
            raise HTTPException(status_code=404, detail="PDF not found")
        file_path = _label_pdf_file_path(user_id, file_id)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="PDF not found")
        return FileResponse(
            path=file_path,
            filename=f"labels_{file_id[:8]}_58x40.pdf",
            media_type="application/pdf",
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


def _product_card_order_history(session: Session, user_id: str, wb_article: str) -> list[dict]:
    if not hasattr(session, "execute"):
        return []
    runs = session.execute(
        select(WorkflowRunModel)
        .where(WorkflowRunModel.user_id == user_id)
        .where(WorkflowRunModel.source_url == f"/product-cards/{wb_article}")
        .order_by(WorkflowRunModel.created_at.desc())
    ).scalars().all()
    return [_serialize_product_card_order_run(run, session) for run in runs]


def _product_card_item_mark_codes(session: Session, item: WorkflowRunItemModel) -> list[str]:
    mark_codes = session.execute(
        select(MarkCodeModel)
        .where(MarkCodeModel.run_item_id == item.id)
        .order_by(MarkCodeModel.position)
    ).scalars().all()
    codes = extract_gs1_mark_codes("\n".join(mark_code.mark_code for mark_code in mark_codes if mark_code.mark_code))
    if codes:
        return codes

    artifact = session.execute(
        select(ArtifactModel)
        .where(ArtifactModel.run_item_id == item.id)
        .where(ArtifactModel.kind == "csv")
    ).scalars().first()
    if artifact is None or not artifact.file_path:
        return []
    try:
        text = Path(artifact.file_path).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    return extract_gs1_mark_codes(text)


def _product_card_ready_to_print_counts(session: Session, user_id: str, wb_article: str) -> dict[str, int]:
    if not hasattr(session, "execute"):
        return {}
    rows = session.execute(
        select(WorkflowRunItemModel, TeksherOperationModel)
        .join(WorkflowRunModel, WorkflowRunModel.id == WorkflowRunItemModel.run_id)
        .outerjoin(
            TeksherOperationModel,
            (TeksherOperationModel.run_item_id == WorkflowRunItemModel.id)
            & (TeksherOperationModel.operation_kind == "marking"),
        )
        .where(WorkflowRunModel.user_id == user_id)
        .where(WorkflowRunModel.source_url == f"/product-cards/{wb_article}")
        .order_by(WorkflowRunModel.created_at.desc(), WorkflowRunItemModel.created_at.desc())
    ).all()
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for item, marking in rows:
        size_key = _product_card_size_key(item.size)
        if not size_key or size_key in seen:
            continue
        seen.add(size_key)
        counts[size_key] = (
            len(_product_card_item_mark_codes(session, item))
            if marking is not None and str(marking.status or "").upper() == "ACCEPTED"
            else 0
        )
    return counts


def _apply_product_card_ready_to_print_counts(product_card, counts: dict[str, int]):
    rows = [
        replace(row, print_count=max(int(counts.get(_product_card_size_key(row.wb_size), 0) or 0), 0))
        for row in product_card.rows
    ]
    return replace(product_card, rows=rows)


def _create_product_card_label_pdf(
    request: Request,
    session: Session,
    user_id: str,
    product_card,
    template: str,
) -> dict:
    sources = _product_card_label_print_sources(session, user_id, product_card)
    if not sources:
        raise ValueError("Нет готовых к печати этикеток для этой карточки.")

    total = sum(len(source["mark_codes"]) for source in sources)
    label_settings = _product_card_label_settings(session, user_id)
    labels = []
    for source in sources:
        item = source["item"]
        row = source["row"]
        for code in source["mark_codes"]:
            labels.append(
                make_label_record(
                    template=template,
                    item_name=_product_card_label_item_name(product_card, row),
                    vendor_code=item.vendor_code or row.vendor_article or product_card.wb_summary.seller_article,
                    size=item.size or row.wb_size,
                    color=_lowercase_text(row.color or product_card.wb_summary.color),
                    composition=row.composition or product_card.wb_summary.composition,
                    wb_barcode=item.barcode or row.barcode,
                    mark_code=code,
                    unit_count="1",
                    index=len(labels) + 1,
                    total=total,
                    supplier_name=label_settings["supplier_name"],
                    production_date=label_settings["production_date"],
                    country_of_origin=row.country or product_card.wb_summary.country,
                    brand=row.trademark or product_card.wb_summary.brand,
                    supplier_address=label_settings["production_address"],
                )
            )

    pdf = render_labels_pdf(labels, template=template)
    file_id = uuid.uuid4().hex
    file_name = f"wb_{product_card.wb_article}_{file_id[:8]}_58x40.pdf"
    file_path = _label_pdf_file_path(user_id, file_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(pdf)

    for source in sources:
        item = source["item"]
        row = source["row"]
        session.add(
            LabelPrintJobModel(
                user_id=user_id,
                wb_article=product_card.wb_article,
                wb_size=item.size or row.wb_size,
                gtin=item.gtin,
                barcode=item.barcode or row.barcode,
                vendor_code=item.vendor_code or row.vendor_article or product_card.wb_summary.seller_article,
                quantity=len(source["mark_codes"]),
                template=template,
                file_id=file_id,
                file_name=file_name,
                status="created",
            )
        )

    session.flush()
    return {
        "ok": True,
        "message": f"PDF этикеток создан: {total}.",
        "template": template,
        "download_url": str(request.url_for("download_label_pdf_api", file_id=file_id)),
        "file_id": file_id,
        "file_name": file_name,
        "labels_count": total,
        "pages_count": _pdf_page_count(pdf),
        "history": _product_card_label_print_history(request, session, user_id, product_card.wb_article),
    }


def _product_card_label_settings(session: Session, user_id: str) -> dict[str, str]:
    result = {
        "supplier_name": "",
        "production_address": "",
        "production_date": date.today().strftime("%d.%m.%Y"),
    }
    if not hasattr(session, "query"):
        return result
    try:
        settings = get_or_create_settings(session, user_id)
    except Exception:
        return result
    result["supplier_name"] = str(settings.supplier_name or "").strip()
    result["production_address"] = str(settings.production_address or "").strip()
    return result


def _product_card_label_item_name(product_card, row) -> str:
    return _capitalize_text(
        getattr(row, "product_type", "")
        or product_card.wb_summary.seller_category
        or product_card.wb_summary.name
    )


def _capitalize_text(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    return lowered[:1].upper() + lowered[1:]


def _uppercase_text(value: str) -> str:
    return str(value or "").strip().upper()


def _lowercase_text(value: str) -> str:
    return str(value or "").strip().lower()


def _product_card_label_print_sources(session: Session, user_id: str, product_card) -> list[dict]:
    if not hasattr(session, "execute"):
        return []
    rows_by_size = {_product_card_size_key(row.wb_size): row for row in product_card.rows}
    rows = session.execute(
        select(WorkflowRunItemModel, TeksherOperationModel)
        .join(WorkflowRunModel, WorkflowRunModel.id == WorkflowRunItemModel.run_id)
        .outerjoin(
            TeksherOperationModel,
            (TeksherOperationModel.run_item_id == WorkflowRunItemModel.id)
            & (TeksherOperationModel.operation_kind == "marking"),
        )
        .where(WorkflowRunModel.user_id == user_id)
        .where(WorkflowRunModel.source_url == f"/product-cards/{product_card.wb_article}")
        .order_by(WorkflowRunModel.created_at.desc(), WorkflowRunItemModel.created_at.desc())
    ).all()

    sources: list[dict] = []
    seen: set[str] = set()
    for item, marking in rows:
        size_key = _product_card_size_key(item.size)
        if not size_key or size_key in seen:
            continue
        seen.add(size_key)
        if marking is None or str(marking.status or "").upper() != "ACCEPTED":
            continue
        row = rows_by_size.get(size_key)
        if row is None:
            continue
        codes = _product_card_item_mark_codes(session, item)
        if not codes:
            continue
        sources.append({"item": item, "row": row, "mark_codes": codes})
    return sources


def _product_card_label_print_history(request: Request, session: Session, user_id: str, wb_article: str) -> list[dict]:
    if not hasattr(session, "execute"):
        return []
    rows = session.execute(
        select(LabelPrintJobModel)
        .where(LabelPrintJobModel.user_id == user_id)
        .where(LabelPrintJobModel.wb_article == wb_article)
        .order_by(LabelPrintJobModel.created_at.desc())
    ).scalars().all()
    return [_serialize_product_card_label_print(request, row) for row in rows]


def _serialize_product_card_label_print(request: Request, row: LabelPrintJobModel) -> dict:
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "size": row.wb_size,
        "gtin": row.gtin,
        "barcode": row.barcode,
        "vendor_code": row.vendor_code,
        "quantity": row.quantity,
        "template": _product_card_label_template_label(row.template),
        "status": row.status,
        "error": row.error,
        "file_id": row.file_id,
        "file_name": row.file_name or (f"labels_{row.file_id[:8]}_58x40.pdf" if row.file_id else ""),
        "download_url": str(request.url_for("download_label_pdf_api", file_id=row.file_id)) if row.file_id else "",
    }


def _product_card_label_template(value: str) -> str:
    key = str(value or "srad").strip().casefold()
    aliases = {
        "srad": "srad",
        "combined": "srad",
        "58x40_full": "srad",
        "simple": "simple",
        "58x40_simple": "simple",
        "medium": "medium",
        "58x40_medium": "medium",
    }
    if key not in aliases:
        raise ValueError("Неизвестный шаблон печати этикеток.")
    return aliases[key]


def _product_card_label_template_label(value: str) -> str:
    labels = {"srad": "SRad", "simple": "Simple", "medium": "Medium"}
    return labels.get(_product_card_label_template(value), "SRad")


def _product_card_size_key(value: str) -> str:
    return str(value or "").strip().casefold()


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


def _serialize_product_card_order_run(run: WorkflowRunModel, session: Session) -> dict:
    items = session.execute(
        select(WorkflowRunItemModel)
        .where(WorkflowRunItemModel.run_id == run.id)
        .order_by(WorkflowRunItemModel.size)
    ).scalars().all()
    return {
        "run": _serialize_run(run),
        "items": [_serialize_product_card_order_item(item, session) for item in items],
    }


def _serialize_product_card_order_item(item: WorkflowRunItemModel, session: Session) -> dict:
    artifact = session.execute(
        select(ArtifactModel)
        .where(ArtifactModel.run_item_id == item.id)
        .where(ArtifactModel.kind == "csv")
    ).scalars().first()
    operations = session.execute(
        select(TeksherOperationModel).where(TeksherOperationModel.run_item_id == item.id)
    ).scalars().all()
    operations_by_kind = {operation.operation_kind: operation for operation in operations}
    return {
        "id": item.id,
        "size": item.size,
        "gtin": item.gtin,
        "quantity": item.quantity,
        "document_number": item.document_number,
        "status": item.status,
        "status_text": _product_card_order_status_text(item, operations_by_kind),
        "error": item.error,
        "artifact_id": artifact.id if artifact else None,
        "artifact_name": artifact.file_name if artifact else None,
        "operations": [
            {
                "kind": operation.operation_kind,
                "external_operation_id": operation.external_operation_id,
                "status": operation.status,
                "end_at": operation.end_at,
            }
            for operation in operations
        ],
    }


def _product_card_order_status_text(item: WorkflowRunItemModel, operations_by_kind: dict[str, TeksherOperationModel]) -> str:
    if item.status == "failed":
        return item.error or "Ошибка выполнения операции."
    if item.status in {"pending", "created"}:
        return "Ожидает запуска"
    if item.status in {"order_running", "order_created"}:
        return "Эмиссия выполняется"
    if item.status == "order_completed":
        return "Эмиссия выполнена"
    if item.status in {"marking_running", "marking_created"}:
        return "Нанесение выполняется"
    if item.status in {"marking_completed", "csv_saved"}:
        return "Нанесение выполнено"
    if item.status == "transgran_running":
        return "Трансгран создается"
    if item.status == "completed":
        transgran = operations_by_kind.get("transgran")
        if transgran is not None and transgran.status == "skipped":
            return "Нанесение выполнено"
        return "Трансгран создан"
    return item.status


def _label_api_auth_error(request: Request) -> JSONResponse | None:
    configured = load_config().label_api_token.strip()
    if not configured:
        return _label_api_error("Label API token is not configured.", 503)

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(token.strip(), configured):
        return _label_api_error("Unauthorized", 401)
    return None


async def _label_api_json_payload(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError as exc:
        raise ValueError("JSON body is required.") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object.")
    return payload


def _label_api_error(message: str, status_code: int = 400, **extra) -> JSONResponse:
    payload = {"status": "error", "message": message}
    payload.update({key: value for key, value in extra.items() if value})
    return JSONResponse(payload, status_code=status_code)


def _label_api_setup_required(nm_id: str, wb_store_id: str, message: str) -> dict:
    return {
        "status": "setup_required",
        "message": message or "Не найден мэппинг или Текшер недоступен. Выполните настройки",
        "settingsUrl": _label_api_settings_url(nm_id, wb_store_id),
    }


def _label_api_settings_url(nm_id: str, wb_store_id: str) -> str:
    config = load_config()
    base_url = config.app_base_url.rstrip("/") or "http://localhost:8000"
    path = f"/product-cards/{_label_api_text(nm_id)}" if _label_api_text(nm_id) else "/settings"
    fragment = f"#{_label_api_text(wb_store_id)}" if _label_api_text(wb_store_id) else ""
    return f"{base_url}{path}{fragment}"


def _label_api_store_context(session: Session, wb_store_id: str) -> tuple[UserModel | None, AppSettingsModel | None]:
    store_id = _label_api_text(wb_store_id)
    if not store_id:
        return None, None

    settings = get_settings_by_store_id(session, store_id)
    user = session.execute(
        select(UserModel).where(UserModel.wb_store_id == store_id).order_by(UserModel.created_at.asc())
    ).scalars().first()
    if user is None and settings is not None and settings.user_id:
        user = session.get(UserModel, settings.user_id)
    if user is not None and settings is None:
        settings = get_or_create_settings(session, user.id)
    if user is not None and settings is not None and not settings.user_id:
        settings.user_id = user.id
        session.add(settings)
    return user, settings


def _label_api_settings_incomplete(settings: AppSettingsModel) -> bool:
    return not all(
        [
            _label_api_text(settings.wb_api_token),
            _label_api_text(settings.teksher_username),
            _label_api_text(settings.teksher_password),
            _label_api_text(settings.teksher_transgran_recipient_name),
            _label_api_text(settings.teksher_transgran_recipient_inn),
            _label_api_text(settings.teksher_transgran_recipient_kpp),
        ]
    )


def _label_api_missing_mapping_sizes(session: Session, user_id: str, nm_id: str, sizes: list[str]) -> list[str]:
    _, rows = teksher_mapping_service.latest_payload(session, user_id, nm_id)
    rows_by_size = {_product_card_size_key(row.get("wb_size", "")): row for row in rows if row.get("gtin")}
    return [size for size in sizes if _product_card_size_key(size) not in rows_by_size]


def _normalize_label_api_print_payload(payload: dict) -> dict:
    request_id = _label_api_text(payload.get("requestId"))
    wb_store_id = _label_api_text(payload.get("wbStoreId"))
    nm_id = _label_api_text(payload.get("nmId"))
    if not request_id:
        raise ValueError("requestId is required.")
    if not wb_store_id:
        raise ValueError("wbStoreId is required.")
    if not nm_id:
        raise ValueError("nmId is required.")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("items must be an array.")
    items: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        size = _label_api_text(item.get("size"))
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if not size or quantity <= 0:
            continue
        items.append({"size": size, "quantity": quantity})
    if not items:
        raise ValueError("items must contain at least one row with quantity > 0.")

    return {
        "requestId": request_id,
        "wbStoreId": wb_store_id,
        "storeName": _label_api_text(payload.get("storeName")),
        "nmId": nm_id,
        "vendorCode": _label_api_text(payload.get("vendorCode")),
        "template": _product_card_label_template(_label_api_text(payload.get("template")) or "srad"),
        "items": items,
    }


def _label_api_request_hash(payload: dict) -> str:
    stable = {
        **payload,
        "items": sorted(payload["items"], key=lambda item: (item["size"], item["quantity"])),
    }
    encoded = json.dumps(stable, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _label_api_existing_job(session: Session, wb_store_id: str, request_id: str) -> LabelApiJobModel | None:
    return session.execute(
        select(LabelApiJobModel)
        .where(LabelApiJobModel.wb_store_id == wb_store_id)
        .where(LabelApiJobModel.request_id == request_id)
    ).scalars().first()


def _label_api_workflow_rows(mapping_rows: list[dict], items: list[dict]) -> list[dict]:
    mapping_by_size = {_product_card_size_key(row.get("wb_size", "")): row for row in mapping_rows if row.get("gtin")}
    rows: list[dict] = []
    for item in items:
        mapping = mapping_by_size.get(_product_card_size_key(item["size"]))
        if not mapping:
            continue
        rows.append(
            {
                "size": item["size"],
                "gtin": _label_api_text(mapping.get("gtin")),
                "quantity": int(item["quantity"]),
                "transgran": True,
            }
        )
    return rows


def _label_api_missing_item_sizes(workflow_rows: list[dict], items: list[dict]) -> list[str]:
    found = {_product_card_size_key(row.get("size", "")) for row in workflow_rows if row.get("gtin")}
    return [item["size"] for item in items if _product_card_size_key(item["size"]) not in found]


def _refresh_label_api_job(request: Request, session: Session, job: LabelApiJobModel) -> None:
    if job.status in {"done", "error"}:
        return
    if not job.run_id:
        job.status = "error"
        job.error = "Workflow run is not linked."
        for row in job.rows:
            row.status = "error"
            row.error_message = job.error
        session.flush()
        return

    run = session.get(WorkflowRunModel, job.run_id)
    if run is None:
        job.status = "error"
        job.error = "Workflow run not found."
        for row in job.rows:
            row.status = "error"
            row.error_message = job.error
        session.flush()
        return

    items_by_size = {_product_card_size_key(item.size): item for item in run.items}
    has_failed = False
    all_completed = bool(job.rows)
    for row in job.rows:
        item = items_by_size.get(_product_card_size_key(row.size))
        if item is None:
            row.status = "emission"
            all_completed = False
            continue
        row.workflow_run_item_id = item.id
        if item.status == "failed":
            row.status = "error"
            row.error_message = item.error or "Не удалось создать этикетки"
            has_failed = True
            all_completed = False
            continue
        if item.status == "completed":
            row.status = "ready" if job.pdf_file_id else "transgran"
            row.error_message = ""
            continue
        row.status = _label_api_row_status(item.status)
        row.error_message = ""
        all_completed = False

    if has_failed or run.status == "partial_failed":
        job.status = "error"
        job.error = job.error or "Одна или несколько строк завершились ошибкой."
        for row in job.rows:
            if row.status != "ready":
                row.status = "error" if row.status != "ready" else row.status
                row.error_message = row.error_message or job.error
        session.flush()
        return

    if all_completed and not job.pdf_file_id:
        try:
            _create_label_api_job_pdf(request, session, job)
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
            for row in job.rows:
                row.status = "error"
                row.error_message = job.error
            session.flush()
            return

    if job.pdf_file_id:
        job.status = "done"
        job.error = ""
        pdf_url = _label_api_file_url(request, job.pdf_file_id)
        job.pdf_url = pdf_url
        for row in job.rows:
            row.status = "ready"
            row.pdf_url = pdf_url
            row.error_message = ""
    else:
        job.status = "processing" if run.status == "running" else "queued"
    session.flush()


def _label_api_row_status(item_status: str) -> str:
    if item_status in {"pending", "created", "order_running", "order_created", "order_completed"}:
        return "emission"
    if item_status in {"marking_running", "marking_created", "marking_completed", "csv_saved"}:
        return "applying"
    if item_status == "transgran_running":
        return "transgran"
    return "emission"


def _create_label_api_job_pdf(request: Request, session: Session, job: LabelApiJobModel) -> None:
    if not job.user_id:
        raise ValueError("Job user is not linked.")
    settings = get_or_create_settings(session, job.user_id)
    config = settings_to_app_config(settings)
    product_card = product_card_service.build_template(job.nm_id, config)
    version, mapping_rows = teksher_mapping_service.latest_payload(session, job.user_id, product_card.wb_article)
    product_card = teksher_mapping_service.apply_payload(product_card, mapping_rows, version)

    product_rows_by_size = {_product_card_size_key(row.wb_size): row for row in product_card.rows}
    job_rows_by_size = {_product_card_size_key(row.size): row for row in job.rows}
    label_settings = _product_card_label_settings(session, job.user_id)
    run_items = session.execute(
        select(WorkflowRunItemModel)
        .where(WorkflowRunItemModel.run_id == job.run_id)
        .order_by(WorkflowRunItemModel.size)
    ).scalars().all()

    sources: list[dict] = []
    total = 0
    for item in run_items:
        size_key = _product_card_size_key(item.size)
        job_row = job_rows_by_size.get(size_key)
        product_row = product_rows_by_size.get(size_key)
        if job_row is None or product_row is None:
            continue
        codes = _product_card_item_mark_codes(session, item)
        if not codes:
            raise ValueError(f"Нет кодов маркировки для размера {item.size}.")
        sources.append({"item": item, "row": product_row, "job_row": job_row, "mark_codes": codes})
        total += len(codes)

    if not sources:
        raise ValueError("Нет готовых кодов маркировки для печати.")

    labels = []
    for source in sources:
        item = source["item"]
        row = source["row"]
        for code in source["mark_codes"]:
            labels.append(
                make_label_record(
                    template=job.template,
                    item_name=_product_card_label_item_name(product_card, row),
                    vendor_code=item.vendor_code or row.vendor_article or product_card.wb_summary.seller_article,
                    size=item.size or row.wb_size,
                    color=_lowercase_text(row.color or product_card.wb_summary.color),
                    composition=row.composition or product_card.wb_summary.composition,
                    wb_barcode=item.barcode or row.barcode,
                    mark_code=code,
                    unit_count="1",
                    index=len(labels) + 1,
                    total=total,
                    supplier_name=label_settings["supplier_name"],
                    production_date=label_settings["production_date"],
                    country_of_origin=row.country or product_card.wb_summary.country,
                    brand=row.trademark or product_card.wb_summary.brand,
                    supplier_address=label_settings["production_address"],
                )
            )

    pdf = render_labels_pdf(labels, template=job.template)
    file_id = uuid.uuid4().hex
    file_name = f"wb_{product_card.wb_article}_{file_id[:8]}_58x40.pdf"
    file_path = _label_pdf_file_path(job.user_id, file_id)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(pdf)

    for source in sources:
        item = source["item"]
        row = source["row"]
        job_row = source["job_row"]
        session.add(
            LabelPrintJobModel(
                user_id=job.user_id,
                wb_article=product_card.wb_article,
                wb_size=item.size or row.wb_size,
                gtin=item.gtin,
                barcode=item.barcode or row.barcode,
                vendor_code=item.vendor_code or row.vendor_article or product_card.wb_summary.seller_article,
                quantity=len(source["mark_codes"]),
                template=job.template,
                file_id=file_id,
                file_name=file_name,
                status="created",
            )
        )
        job_row.status = "ready"
        job_row.pdf_url = _label_api_file_url(request, file_id)

    job.pdf_file_id = file_id
    job.pdf_url = _label_api_file_url(request, file_id)
    session.flush()


def _serialize_label_api_job(request: Request, job: LabelApiJobModel) -> dict:
    rows = sorted(job.rows, key=lambda row: _label_api_size_sort_key(row.size))
    payload = {
        "jobId": job.id,
        "status": job.status,
        "rows": [_serialize_label_api_job_row(row) for row in rows],
    }
    if job.pdf_file_id:
        payload["pdfUrl"] = _label_api_file_url(request, job.pdf_file_id)
    if job.error:
        payload["errorMessage"] = job.error
    return payload


def _serialize_label_api_job_row(row: LabelApiJobRowModel) -> dict:
    payload = {
        "size": row.size,
        "quantity": row.quantity,
        "status": row.status,
    }
    if row.pdf_url:
        payload["pdfUrl"] = row.pdf_url
    if row.error_message:
        payload["errorMessage"] = row.error_message
    return payload


def _label_api_file_url(request: Request, file_id: str) -> str:
    return str(request.url_for("download_label_api_pdf", file_id=file_id))


def _label_api_sizes(value) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _label_api_text(item)
        key = _product_card_size_key(text)
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _label_api_text(value) -> str:
    return str(value or "").strip()


def _label_api_size_sort_key(value: str) -> tuple[int, str]:
    text = _label_api_text(value)
    try:
        return (0, f"{int(text):06d}")
    except ValueError:
        return (1, text.casefold())


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
    return _label_form_from_inputs()


def _label_form_from_inputs(
    *,
    template: str = "srad",
    item_name: str = "",
    vendor_code: str = "",
    size: str = "",
    color: str = "",
    composition: str = "",
    wb_barcode: str = "",
    mark_codes: str = "",
    unit_count: str = "1",
    copies: int = 1,
    note_text: str = "",
    supplier_name: str = "",
    production_date: str = "",
    country_of_origin: str = "",
    brand: str = "",
    supplier_address: str = "",
) -> dict:
    return {
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
        "supplier_name": supplier_name,
        "production_date": production_date,
        "country_of_origin": country_of_origin,
        "brand": brand,
        "supplier_address": supplier_address,
    }


def _payload_value(payload: dict, *keys: str):
    for key in keys:
        if key in payload:
            return payload[key]
    casefolded = {str(key).casefold(): value for key, value in payload.items()}
    for key in keys:
        value = casefolded.get(key.casefold())
        if value is not None:
            return value
    return None


def _payload_text(payload: dict, *keys: str) -> str:
    value = _payload_value(payload, *keys)
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(str(item).strip() for item in value if item is not None).strip()
    return str(value).strip()


def _payload_mark_codes(payload: dict) -> list[str]:
    value = _payload_value(payload, "mark_codes", "codes", "chz_codes", "коды_чз", "Коды ЧЗ")
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return [line.strip() for line in str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


def _template_requires_mark_codes(template: str) -> bool:
    return str(template or "").strip().lower() in {
        "srad",
        "combined",
        "58x40_full",
        "wb_chz_58x40",
        "simple",
        "58x40_simple",
        "medium",
        "58x40_medium",
        "chz",
        "58x40_chz",
    }


def _label_pdf_file_path(user_id: str, file_id: str) -> Path:
    root = load_config().resolved_artifact_storage_dir() / "label-pdfs" / _safe_path_token(user_id)
    return root / f"{file_id}.pdf"


def _safe_path_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return token or "user"


def _pdf_page_count(pdf: bytes) -> int:
    return pdf.count(b"/Type /Page") - pdf.count(b"/Type /Pages")


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


def _product_mapping_save_message(version: int, rows_saved: int, created_gtins: list[str]) -> str:
    if created_gtins:
        gtins = ", ".join(created_gtins)
        return f"Карточки для GTIN: {gtins} созданы в Текшер. Мэппинг сохранен: версия {version}, строк {rows_saved}."
    return f"Мэппинг сохранен: версия {version}, строк {rows_saved}. Новые карточки в Текшер не создавались."


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


def _build_run_labels(
    session: Session,
    run_id: str,
    user_id: str,
    template: str,
) -> tuple[WorkflowRunModel, list]:
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
    return run, labels
