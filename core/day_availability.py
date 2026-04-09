from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .models import AgendaSettings, ManualClosure, OfficialHoliday


@dataclass(frozen=True, slots=True)
class ResolvedDayAvailability:
    day: date
    status: str
    label: str
    is_working_day: bool
    manual_closure: ManualClosure | None = None
    official_holiday: OfficialHoliday | None = None


class DayAvailabilityResolver:
    WORKING_DAY = "working_day"
    NON_WORKING_SATURDAY = "non_working_saturday"
    NON_WORKING_SUNDAY = "non_working_sunday"
    OFFICIAL_HOLIDAY = "official_holiday"
    MANUAL_CLOSURE = "manual_closure"

    def __init__(
        self,
        *,
        agenda_settings: AgendaSettings,
        manual_closures: Iterable[ManualClosure] = (),
        official_holidays: Iterable[OfficialHoliday] | None = None,
    ):
        self.agenda_settings = agenda_settings
        self.manual_closures = tuple(manual_closures)
        self.official_holidays = None if official_holidays is None else tuple(official_holidays)
        self._official_holiday_cache = {}

    @classmethod
    def resolve_for_global_agenda(
        cls,
        target_day: date,
        *,
        agenda_settings: AgendaSettings | None = None,
    ) -> ResolvedDayAvailability:
        settings = agenda_settings or AgendaSettings.get_solo()
        manual_closures = ManualClosure.objects.covering_day(target_day).order_by(
            "start_date",
            "end_date",
            "id",
        )
        official_holidays = OfficialHoliday.objects.on_day(target_day).order_by("day", "id")
        return cls(
            agenda_settings=settings,
            manual_closures=manual_closures,
            official_holidays=official_holidays,
        ).resolve(target_day)

    def resolve(self, target_day: date) -> ResolvedDayAvailability:
        manual_closure = self._manual_closure_for_day(target_day)
        if manual_closure is not None:
            return ResolvedDayAvailability(
                day=target_day,
                status=self.MANUAL_CLOSURE,
                label=manual_closure.display_label,
                is_working_day=False,
                manual_closure=manual_closure,
            )

        official_holiday = self._official_holiday_for_day(target_day)
        if official_holiday is not None and self.agenda_settings.official_holidays_non_working:
            return ResolvedDayAvailability(
                day=target_day,
                status=self.OFFICIAL_HOLIDAY,
                label=official_holiday.name,
                is_working_day=False,
                official_holiday=official_holiday,
            )

        if target_day.weekday() == 5 and self.agenda_settings.saturdays_non_working:
            return ResolvedDayAvailability(
                day=target_day,
                status=self.NON_WORKING_SATURDAY,
                label="Sabado no laborable",
                is_working_day=False,
                official_holiday=official_holiday,
            )

        if target_day.weekday() == 6 and self.agenda_settings.sundays_non_working:
            return ResolvedDayAvailability(
                day=target_day,
                status=self.NON_WORKING_SUNDAY,
                label="Domingo no laborable",
                is_working_day=False,
                official_holiday=official_holiday,
            )

        return ResolvedDayAvailability(
            day=target_day,
            status=self.WORKING_DAY,
            label="Laborable",
            is_working_day=True,
            official_holiday=official_holiday,
        )

    def _manual_closure_for_day(self, target_day: date) -> ManualClosure | None:
        for manual_closure in self.manual_closures:
            if manual_closure.covers_day(target_day):
                return manual_closure
        return None

    def _official_holiday_for_day(self, target_day: date) -> OfficialHoliday | None:
        if self.official_holidays is not None:
            for official_holiday in self.official_holidays:
                if official_holiday.day == target_day:
                    return official_holiday
            return None

        if target_day not in self._official_holiday_cache:
            self._official_holiday_cache[target_day] = OfficialHoliday.objects.on_day(target_day).first()
        return self._official_holiday_cache[target_day]
