from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction

from core.models import OfficialHoliday


class BoeSyncError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class BoeHolidayResolution:
    identifier: str
    title: str
    url_html: str


@dataclass(frozen=True, slots=True)
class OfficialHolidayImport:
    day: date
    name: str


@dataclass(frozen=True, slots=True)
class OfficialHolidaySyncResult:
    target_year: int
    resolution: BoeHolidayResolution
    created_count: int
    skipped_existing_count: int
    error_count: int
    reconciled_count: int = 0


class BoeNationalHolidaySyncService:
    BOE_SUMMARY_API_URL = "https://www.boe.es/datosabiertos/api/boe/sumario/{date_code}"
    SUMMARY_ACCEPT = "application/json"
    NATIONAL_MARKERS = {"*", "**"}
    MONTHS = {
        "enero": 1,
        "febrero": 2,
        "marzo": 3,
        "abril": 4,
        "mayo": 5,
        "junio": 6,
        "julio": 7,
        "agosto": 8,
        "septiembre": 9,
        "octubre": 10,
        "noviembre": 11,
        "diciembre": 12,
    }

    def __init__(self, *, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def fetch_national_holidays(self, target_year: int) -> tuple[BoeHolidayResolution, list[OfficialHolidayImport]]:
        resolution = self.find_resolution(target_year)
        resolution_html = self.fetch_resolution_html(resolution.url_html)
        holidays = self.extract_national_holidays(target_year, resolution_html)
        if not holidays:
            raise BoeSyncError(
                f"No se han podido extraer festivos nacionales del BOE para el año {target_year}."
            )
        return resolution, holidays

    def find_resolution(self, target_year: int) -> BoeHolidayResolution:
        title_snippet = self._normalize_text(f"relación de fiestas laborales para el año {target_year}")
        previous_year = target_year - 1
        current_day = date(previous_year, 1, 1)
        end_day = date(previous_year, 12, 31)

        while current_day <= end_day:
            summary_payload = self.fetch_summary_payload(current_day)
            if summary_payload is not None:
                resolution = self.extract_resolution_from_summary(summary_payload, title_snippet)
                if resolution is not None:
                    return resolution
            current_day += timedelta(days=1)

        raise BoeSyncError(
            f"No se ha encontrado en el BOE la resolución de fiestas laborales para el año {target_year}."
        )

    def fetch_summary_payload(self, target_day: date):
        response = self.session.get(
            self.BOE_SUMMARY_API_URL.format(date_code=target_day.strftime("%Y%m%d")),
            headers={"Accept": self.SUMMARY_ACCEPT},
            timeout=20,
        )

        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BoeSyncError(
                f"El sumario diario del BOE para {target_day:%Y-%m-%d} ha devuelto {response.status_code}."
            )

        payload = response.json()
        status = str(payload.get("status", {}).get("code", ""))
        if status != "200":
            raise BoeSyncError(
                f"El sumario diario del BOE para {target_day:%Y-%m-%d} no se pudo consultar correctamente."
            )
        return payload.get("data", {}).get("sumario", {})

    def extract_resolution_from_summary(
        self,
        summary_payload,
        normalized_title_snippet: str,
    ) -> BoeHolidayResolution | None:
        for diario in self._as_list(summary_payload.get("diario")):
            for seccion in self._as_list(diario.get("seccion")):
                for departamento in self._as_list(seccion.get("departamento")):
                    for epigrafe in self._as_list(departamento.get("epigrafe")):
                        for item in self._as_list(epigrafe.get("item")):
                            title = self._clean_text(item.get("titulo", ""))
                            normalized_title = self._normalize_text(title)
                            if normalized_title_snippet not in normalized_title:
                                continue
                            identifier = item.get("identificador", "")
                            url_html = item.get("url_html", "")
                            if identifier.startswith("BOE-A-") and url_html:
                                return BoeHolidayResolution(
                                    identifier=identifier,
                                    title=title,
                                    url_html=url_html,
                                )
        return None

    def fetch_resolution_html(self, url_html: str) -> str:
        response = self.session.get(url_html, timeout=20)
        if response.status_code != 200:
            raise BoeSyncError(f"No se ha podido descargar la resolución del BOE ({response.status_code}).")
        return response.text

    def extract_national_holidays(
        self,
        target_year: int,
        resolution_html: str,
    ) -> list[OfficialHolidayImport]:
        soup = BeautifulSoup(resolution_html, "html.parser")
        table = soup.find("table")
        if table is None:
            raise BoeSyncError("La resolución del BOE no contiene una tabla de festivos reconocible.")

        current_month = None
        holidays = []

        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue

            first_cell_text = self._clean_text(cells[0].get_text(" ", strip=True))
            normalized_first_cell = self._normalize_text(first_cell_text.rstrip("."))

            if normalized_first_cell in self.MONTHS:
                current_month = self.MONTHS[normalized_first_cell]
                continue

            match = re.match(r"^(?P<day>\d{1,2})\s+(?P<name>.+)$", first_cell_text)
            if match is None or current_month is None:
                continue

            markers = [self._clean_text(cell.get_text(" ", strip=True)) for cell in cells[1:]]
            if markers and all(marker in self.NATIONAL_MARKERS for marker in markers):
                holidays.append(
                    OfficialHolidayImport(
                        day=date(target_year, current_month, int(match.group("day"))),
                        name=match.group("name").rstrip(".").strip(),
                    )
                )

        return holidays

    def _clean_text(self, value: str) -> str:
        return " ".join(value.split())

    def _normalize_text(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
        return ascii_value.lower().strip()

    def _as_list(self, value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]


def import_boe_national_holidays(
    target_year: int,
    *,
    service: BoeNationalHolidaySyncService | None = None,
    error_reporter=None,
) -> OfficialHolidaySyncResult:
    sync_service = service or BoeNationalHolidaySyncService()
    resolution, holidays = sync_service.fetch_national_holidays(target_year)

    created_count = 0
    skipped_existing_count = 0
    error_count = 0
    reconciled_count = 0
    year_start = date(target_year, 1, 1)
    next_year_start = date(target_year + 1, 1, 1)
    authoritative_holidays_by_day = {holiday.day: holiday for holiday in holidays}
    authoritative_days = set(authoritative_holidays_by_day)

    with transaction.atomic():
        synced_holidays_for_year = OfficialHoliday.objects.filter(
            source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
            day__gte=year_start,
            day__lt=next_year_start,
        )
        outdated_synced_holidays = synced_holidays_for_year.exclude(day__in=authoritative_days)
        outdated_count = outdated_synced_holidays.count()
        if outdated_count:
            outdated_synced_holidays.delete()
            reconciled_count += outdated_count

        existing_holidays_by_day = {
            official_holiday.day: official_holiday
            for official_holiday in OfficialHoliday.objects.filter(
                day__gte=year_start,
                day__lt=next_year_start,
            ).order_by("day", "id")
        }

        for holiday in holidays:
            existing_holiday = existing_holidays_by_day.get(holiday.day)
            if existing_holiday is None:
                try:
                    created_holiday = OfficialHoliday.objects.create(
                        day=holiday.day,
                        name=holiday.name,
                        source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
                    )
                except (IntegrityError, ValueError) as error:
                    error_count += 1
                    if error_reporter is not None:
                        error_reporter(f"Error al importar {holiday.day:%Y-%m-%d} ({holiday.name}): {error}")
                else:
                    created_count += 1
                    existing_holidays_by_day[holiday.day] = created_holiday
                continue

            if (
                existing_holiday.source == OfficialHoliday.Source.BOE_NATIONAL_SYNC
                and existing_holiday.name != holiday.name
            ):
                existing_holiday.name = holiday.name
                existing_holiday.save(update_fields=["name"])
                reconciled_count += 1
                continue

            skipped_existing_count += 1

    return OfficialHolidaySyncResult(
        target_year=target_year,
        resolution=resolution,
        created_count=created_count,
        skipped_existing_count=skipped_existing_count,
        error_count=error_count,
        reconciled_count=reconciled_count,
    )


class Command(BaseCommand):
    help = "Import Spanish nationwide official holidays for a year from the BOE into OfficialHoliday."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, required=True, help="Target year to import from the BOE.")

    def handle(self, *args, **options):
        target_year = options["year"]

        try:
            result = import_boe_national_holidays(
                target_year,
                error_reporter=lambda message: self.stderr.write(self.style.ERROR(message)),
            )
        except (requests.RequestException, BoeSyncError) as error:
            raise CommandError(str(error)) from error

        self.stdout.write(f"BOE {result.resolution.identifier}: {result.resolution.title}")
        self.stdout.write(
            self.style.SUCCESS(
                "Importacion completada: "
                f"creados={result.created_count}, "
                f"ignorados_existentes={result.skipped_existing_count}, "
                f"reconciliados={result.reconciled_count}, "
                f"errores={result.error_count}."
            )
        )
