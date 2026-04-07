document.documentElement.classList.add("js");

const syncSlotPickerState = (picker, select) => {
    const selectedValue = select.value;

    picker.querySelectorAll("[data-slot-option]").forEach((option) => {
        const isSelected = option.dataset.slotValue === selectedValue;
        option.classList.toggle("appointment-slot-row--selected", isSelected);

        if (option.matches("button")) {
            option.setAttribute("aria-pressed", isSelected ? "true" : "false");
        }
    });
};

const syncClientCreateLink = (picker, select) => {
    const clientCreateLink = picker.closest("form")?.querySelector("[data-client-create-link]");
    if (!clientCreateLink) {
        return;
    }

    const nextValue = select.value;
    const linkUrl = new URL(clientCreateLink.href, window.location.origin);

    if (nextValue) {
        linkUrl.searchParams.set("slot_time", nextValue);
    } else {
        linkUrl.searchParams.delete("slot_time");
    }

    clientCreateLink.href = `${linkUrl.pathname}${linkUrl.search}${linkUrl.hash}`;
};

const initAppointmentSlotPickers = () => {
    document.querySelectorAll("[data-slot-picker]").forEach((picker) => {
        if (picker.dataset.slotPickerReady === "true") {
            return;
        }

        const select = picker.querySelector('select[name="slot_time"]');
        if (!select) {
            return;
        }

        picker.querySelectorAll("[data-slot-button]").forEach((button) => {
            button.addEventListener("click", () => {
                const nextValue = button.dataset.slotValue;

                if (!nextValue || select.value === nextValue) {
                    syncSlotPickerState(picker, select);
                    syncClientCreateLink(picker, select);
                    return;
                }

                const matchingOption = Array.from(select.options).find(
                    (option) => option.value === nextValue && !option.disabled,
                );

                if (!matchingOption) {
                    syncSlotPickerState(picker, select);
                    syncClientCreateLink(picker, select);
                    return;
                }

                select.value = nextValue;
                select.dispatchEvent(new Event("change", { bubbles: true }));
                syncSlotPickerState(picker, select);
                syncClientCreateLink(picker, select);
            });
        });

        select.addEventListener("change", () => {
            syncSlotPickerState(picker, select);
            syncClientCreateLink(picker, select);
        });

        syncSlotPickerState(picker, select);
        syncClientCreateLink(picker, select);
        picker.dataset.slotPickerReady = "true";
    });
};

const syncAppointmentEditDangerState = (form) => {
    const statusSelect = form.querySelector('select[name="status"]');
    const deleteModeInput = form.querySelector("[data-delete-mode-input]");
    const deleteTrigger = form.querySelector("[data-delete-trigger]");
    const cancelNotice = form.querySelector("[data-cancel-notice]");
    const deleteConfirmation = form.querySelector("[data-delete-confirmation]");
    const isDeleteMode = deleteModeInput?.value === "true";
    const isCancelled = statusSelect?.value === "cancelled";

    if (deleteTrigger) {
        deleteTrigger.hidden = isDeleteMode;
        deleteTrigger.setAttribute("aria-expanded", isDeleteMode ? "true" : "false");
    }

    if (cancelNotice) {
        cancelNotice.hidden = isDeleteMode || !isCancelled;
    }

    if (deleteConfirmation) {
        deleteConfirmation.hidden = !isDeleteMode;
    }
};

const initAppointmentEditForms = () => {
    document.querySelectorAll("[data-appointment-edit-form]").forEach((form) => {
        if (form.dataset.editDangerReady === "true") {
            return;
        }

        const statusSelect = form.querySelector('select[name="status"]');
        const deleteModeInput = form.querySelector("[data-delete-mode-input]");
        const deleteTrigger = form.querySelector("[data-delete-trigger]");
        const dismissTriggers = form.querySelectorAll("[data-delete-dismiss]");
        const saveChangesButton = form.querySelector("[data-save-changes]");

        const setDeleteMode = (isDeleteMode) => {
            if (!deleteModeInput) {
                return;
            }

            deleteModeInput.value = isDeleteMode ? "true" : "false";
            syncAppointmentEditDangerState(form);
        };

        if (deleteTrigger) {
            deleteTrigger.addEventListener("click", (event) => {
                event.preventDefault();
                setDeleteMode(true);
            });
        }

        dismissTriggers.forEach((trigger) => {
            trigger.addEventListener("click", (event) => {
                event.preventDefault();
                setDeleteMode(false);
            });
        });

        if (statusSelect) {
            statusSelect.addEventListener("change", () => syncAppointmentEditDangerState(form));
        }

        if (saveChangesButton) {
            saveChangesButton.addEventListener("click", () => setDeleteMode(false));
        }

        syncAppointmentEditDangerState(form);
        form.dataset.editDangerReady = "true";
    });
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
        initAppointmentSlotPickers();
        initAppointmentEditForms();
    });
} else {
    initAppointmentSlotPickers();
    initAppointmentEditForms();
}
