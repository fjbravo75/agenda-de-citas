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
                    return;
                }

                const matchingOption = Array.from(select.options).find(
                    (option) => option.value === nextValue && !option.disabled,
                );

                if (!matchingOption) {
                    syncSlotPickerState(picker, select);
                    return;
                }

                select.value = nextValue;
                select.dispatchEvent(new Event("change", { bubbles: true }));
                syncSlotPickerState(picker, select);
            });
        });

        select.addEventListener("change", () => syncSlotPickerState(picker, select));

        syncSlotPickerState(picker, select);
        picker.dataset.slotPickerReady = "true";
    });
};

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAppointmentSlotPickers);
} else {
    initAppointmentSlotPickers();
}
