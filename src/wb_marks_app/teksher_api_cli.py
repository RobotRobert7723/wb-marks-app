from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from wb_marks_app.config import load_config
from wb_marks_app.exceptions import AppError
from wb_marks_app.models import AppConfig
from wb_marks_app.services.browser import BrowserSessionManager
from wb_marks_app.services.teksher import TeksherService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Teksher API flows without UI clicks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    issue_parser = subparsers.add_parser("issue", help="Run mark emission -> marking CSV flow.")
    issue_parser.add_argument("--gtin", required=True, help="GTIN to issue.")
    issue_parser.add_argument("--quantity", required=True, type=int, help="How many marking codes to issue.")
    issue_parser.add_argument("--output", help="Path to save the marking CSV.")
    add_common_auth_args(issue_parser)

    full_parser = subparsers.add_parser("full", help="Run issue -> CSV -> transgran in one command.")
    full_parser.add_argument("--gtin", action="append", required=True, dest="gtins", help="GTIN to issue and send to transgran. Repeat for multiple GTINs.")
    full_parser.add_argument("--quantity", action="append", required=True, dest="quantities", type=int, help="Quantity for the matching GTIN. Repeat in the same order as --gtin.")
    full_parser.add_argument("--output", help="Path to save the combined marking CSV.")
    full_parser.add_argument("--document-number", required=True, help="Shipment document number.")
    full_parser.add_argument("--document-date", required=True, help="Document date in ISO format, e.g. 2026-04-24T19:31:39")
    full_parser.add_argument("--shipment-date", required=True, help="Shipment date in ISO format, e.g. 2026-04-24T19:31:41")
    full_parser.add_argument("--recipient-name", help="Override recipient name.")
    full_parser.add_argument("--recipient-inn", help="Override recipient INN.")
    full_parser.add_argument("--recipient-kpp", help="Override recipient KPP.")
    add_common_auth_args(full_parser)

    transgran_parser = subparsers.add_parser("transgran", help="Upload CSV and create a transgran operation.")
    transgran_parser.add_argument("--csv", required=True, help="Path to CSV with marking codes.")
    transgran_parser.add_argument("--gtin", action="append", required=True, dest="gtins", help="GTIN included in the file. Repeat for multiple GTINs.")
    transgran_parser.add_argument("--document-number", required=True, help="Shipment document number.")
    transgran_parser.add_argument("--document-date", required=True, help="Document date in ISO format, e.g. 2026-04-24T19:31:39")
    transgran_parser.add_argument("--shipment-date", required=True, help="Shipment date in ISO format, e.g. 2026-04-24T19:31:41")
    transgran_parser.add_argument("--recipient-name", help="Override recipient name.")
    transgran_parser.add_argument("--recipient-inn", help="Override recipient INN.")
    transgran_parser.add_argument("--recipient-kpp", help="Override recipient KPP.")
    add_common_auth_args(transgran_parser)

    return parser


def add_common_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--token", help="Bearer token for label.teksher.kg.")
    parser.add_argument("--username", help="Teksher username for automatic token refresh.")
    parser.add_argument("--password", help="Teksher password for automatic token refresh.")
    parser.add_argument("--order-timeout", type=int, help="Override Teksher polling timeout in seconds.")


def main() -> None:
    args = build_parser().parse_args()
    config = load_config()
    apply_auth_overrides(config, args)

    browser = BrowserSessionManager(config.resolved_profile_dir())
    service = TeksherService(browser=browser, logger=print)

    if args.command == "issue":
        output_path = _resolve_issue_output(args.output, args.gtin, config)
        result_path = service.run_full_cycle(args.gtin, args.quantity, config, output_path)
        print(f"Saved CSV to {result_path}")
        return

    if args.command == "transgran":
        apply_transgran_overrides(config, args)
        operation_id = service.run_transgran_cycle(
            csv_path=Path(args.csv).expanduser(),
            gtins=args.gtins,
            document_number=args.document_number,
            document_date=parse_iso_datetime(args.document_date),
            shipment_date=parse_iso_datetime(args.shipment_date),
            config=config,
        )
        print(f"Created transgran operation {operation_id}")
        return

    if args.command == "full":
        apply_transgran_overrides(config, args)
        if len(args.gtins) != len(args.quantities):
            raise SystemExit("The number of --gtin and --quantity arguments must match.")

        output_path = _resolve_full_output(args.output, args.gtins, config)
        combined_csv = run_full_flow(
            service=service,
            gtins=args.gtins,
            quantities=args.quantities,
            output_path=output_path,
            document_number=args.document_number,
            document_date=parse_iso_datetime(args.document_date),
            shipment_date=parse_iso_datetime(args.shipment_date),
            config=config,
        )
        print(f"Saved combined CSV to {combined_csv}")
        return

    raise SystemExit(f"Unsupported command: {args.command}")


def apply_auth_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    if getattr(args, "token", None):
        config.teksher_api_token = args.token
    if getattr(args, "username", None):
        config.teksher_username = args.username
    if getattr(args, "password", None):
        config.teksher_password = args.password
    if getattr(args, "order_timeout", None):
        config.step_timeout_seconds = args.order_timeout


def apply_transgran_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    if args.recipient_name:
        config.teksher_transgran_recipient_name = args.recipient_name
    if args.recipient_inn:
        config.teksher_transgran_recipient_inn = args.recipient_inn
    if args.recipient_kpp:
        config.teksher_transgran_recipient_kpp = args.recipient_kpp


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _resolve_issue_output(raw_output: str | None, gtin: str, config: AppConfig) -> Path:
    if raw_output:
        return Path(raw_output).expanduser()
    return config.resolved_output_dir() / f"teksher_{gtin}.csv"


def _resolve_full_output(raw_output: str | None, gtins: list[str], config: AppConfig) -> Path:
    if raw_output:
        return Path(raw_output).expanduser()
    if len(gtins) == 1:
        return config.resolved_output_dir() / f"teksher_{gtins[0]}_full.csv"
    return config.resolved_output_dir() / "teksher_full.csv"


def run_full_flow(
    service: TeksherService,
    gtins: list[str],
    quantities: list[int],
    output_path: Path,
    document_number: str,
    document_date: datetime,
    shipment_date: datetime,
    config: AppConfig,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if len(gtins) == 1:
        return run_resumable_single_flow(
            service=service,
            gtin=gtins[0],
            quantity=quantities[0],
            output_path=output_path,
            document_number=document_number,
            document_date=document_date,
            shipment_date=shipment_date,
            config=config,
        )

    partial_paths: list[Path] = []

    for gtin, quantity in zip(gtins, quantities, strict=True):
        partial_output = output_path.parent / f"{output_path.stem}_{gtin}{output_path.suffix}"
        result_path = service.run_full_cycle(gtin, quantity, config, partial_output)
        partial_paths.append(result_path)

    if len(partial_paths) == 1:
        shutil.copyfile(partial_paths[0], output_path)
    else:
        with output_path.open("wb") as combined:
            for index, partial_path in enumerate(partial_paths):
                payload = partial_path.read_bytes()
                if index > 0 and payload and not payload.startswith(b"\n") and not payload.startswith(b"\r\n"):
                    combined.write(b"\n")
                combined.write(payload.rstrip(b"\r\n"))
            combined.write(b"\n")

    operation_id = service.run_transgran_cycle(
        csv_path=output_path,
        gtins=gtins,
        document_number=document_number,
        document_date=document_date,
        shipment_date=shipment_date,
        config=config,
    )
    print(f"Created transgran operation {operation_id}")
    return output_path


def run_resumable_single_flow(
    service: TeksherService,
    gtin: str,
    quantity: int,
    output_path: Path,
    document_number: str,
    document_date: datetime,
    shipment_date: datetime,
    config: AppConfig,
) -> Path:
    state_path = output_path.with_suffix(".state.json")
    state = load_state(state_path)
    state.setdefault("gtin", gtin)
    state.setdefault("quantity", quantity)
    state.setdefault("output_path", str(output_path))

    if not service._has_valid_token(config):
        service._authenticate(config)

    order_id = str(state.get("order_id") or "")
    if not order_id:
        data = _create_single_order_with_retry(service, gtin, quantity, config)
        order_ids = data.get("data") or {}
        order_id = next(iter(order_ids.keys()), "")
        if not order_id:
            raise SystemExit(f"Teksher did not return an order id: {data}")
        state["order_id"] = order_id
        save_state(state_path, state)
        print(f"Teksher order created: {order_id} for GTIN {gtin}")

    service._wait_for_order_ready(order_id, config)

    marking_operation_id = str(state.get("marking_operation_id") or "")
    if not marking_operation_id:
        marking_operation_id = service._create_utilisation(order_id, config)
        state["marking_operation_id"] = marking_operation_id
        save_state(state_path, state)
        print(f"Teksher marking operation created: {marking_operation_id}")

    service._wait_for_operation_status(marking_operation_id, "ACCEPTED", config)
    if not output_path.exists():
        service.save_operation_csv(marking_operation_id, output_path, config)
    state["csv_saved"] = True
    save_state(state_path, state)

    transgran_operation_id = str(state.get("transgran_operation_id") or "")
    if not transgran_operation_id:
        resolved_document_date, resolved_shipment_date = service.resolve_transgran_dates(
            marking_operation_id=marking_operation_id,
            requested_document_date=document_date,
            requested_shipment_date=shipment_date,
            config=config,
        )
        transgran_operation_id = service.run_transgran_cycle(
            csv_path=output_path,
            gtins=[gtin],
            document_number=document_number,
            document_date=resolved_document_date,
            shipment_date=resolved_shipment_date,
            config=config,
        )
        state["transgran_operation_id"] = transgran_operation_id
        state["document_date"] = resolved_document_date.isoformat(timespec="seconds")
        state["shipment_date"] = resolved_shipment_date.isoformat(timespec="seconds")
        save_state(state_path, state)
        print(f"Created transgran operation {transgran_operation_id}")

    return output_path


def _create_single_order_with_retry(
    service: TeksherService,
    gtin: str,
    quantity: int,
    config: AppConfig,
    max_attempts: int = 4,
) -> dict:
    headers = service._headers(config)
    payload = {
        "countryId": config.teksher_country_id,
        "extension": config.teksher_extension,
        "items": [
            {
                "gtin": gtin,
                "markingCodesAmount": quantity,
                "dataSupplier": "AUTO",
            }
        ],
    }

    last_error: AppError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = service.session.post(
                service._url(config, "/facade/order/api/v1/operations/multi"),
                headers=headers,
                json=payload,
                timeout=30,
            )
            service._raise_for_status(response)
            return service._json(response)
        except AppError as exc:
            last_error = exc
            if attempt == max_attempts or "502" not in str(exc):
                break
            print(
                f"Teksher order create transient error for GTIN {gtin} "
                f"(attempt {attempt}/{max_attempts}): {exc}"
            )
            time.sleep(2.0)

    raise SystemExit(f"Failed to create Teksher order for GTIN {gtin}: {last_error}")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
