from django.utils import timezone

from .models import AgendaSettings


def store_boe_sync_trace(result, *, synced_at=None, agenda_settings=None):
    settings = agenda_settings or AgendaSettings.get_solo()
    settings.last_boe_sync_at = synced_at or timezone.now()
    settings.last_boe_sync_year = result.target_year
    settings.last_boe_sync_resolution_identifier = result.resolution.identifier
    settings.last_boe_sync_resolution_title = result.resolution.title
    settings.last_boe_sync_resolution_url = result.resolution.url_html
    settings.last_boe_sync_created_count = result.created_count
    settings.last_boe_sync_skipped_existing_count = result.skipped_existing_count
    settings.last_boe_sync_error_count = result.error_count
    settings.save(
        update_fields=[
            "last_boe_sync_at",
            "last_boe_sync_year",
            "last_boe_sync_resolution_identifier",
            "last_boe_sync_resolution_title",
            "last_boe_sync_resolution_url",
            "last_boe_sync_created_count",
            "last_boe_sync_skipped_existing_count",
            "last_boe_sync_error_count",
        ]
    )
    return settings


def store_boe_sync_failure_trace(target_year, error_message, *, failed_at=None, agenda_settings=None):
    settings = agenda_settings or AgendaSettings.get_solo()
    settings.last_boe_sync_failure_at = failed_at or timezone.now()
    settings.last_boe_sync_failure_year = target_year
    settings.last_boe_sync_failure_message = error_message or "Error desconocido durante el sync BOE."
    settings.save(
        update_fields=[
            "last_boe_sync_failure_at",
            "last_boe_sync_failure_year",
            "last_boe_sync_failure_message",
        ]
    )
    return settings


def clear_boe_sync_failure_trace(*, agenda_settings=None):
    settings = agenda_settings or AgendaSettings.get_solo()
    settings.last_boe_sync_failure_at = None
    settings.last_boe_sync_failure_year = None
    settings.last_boe_sync_failure_message = ""
    settings.save(
        update_fields=[
            "last_boe_sync_failure_at",
            "last_boe_sync_failure_year",
            "last_boe_sync_failure_message",
        ]
    )
    return settings
