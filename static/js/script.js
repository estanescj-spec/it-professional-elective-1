"use strict";

function formatCurrency(amount) {
  return `₱${amount.toLocaleString("en-PH", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function updatePricePreview(preview) {
  const serviceSelect = document.getElementById(preview.dataset.rateSelect);
  const weightInput = document.getElementById(preview.dataset.weightInput);
  const selectedOption = serviceSelect?.selectedOptions[0];
  const rate = Number(selectedOption?.dataset.rate || 0);
  const rawWeight = weightInput?.value || "";
  const weight = Number(rawWeight);
  const perKgRate = Number(preview.dataset.perKgRate || 0);
  const maximumWeight = Number(preview.dataset.maxWeight || 50);
  const message = preview.querySelector("[data-price-message]");
  if (!rawWeight || !Number.isFinite(weight) || weight <= 0) {
    preview.querySelector("[data-price-base]").textContent = "—";
    preview.querySelector("[data-price-weight]").textContent = "—";
    preview.querySelector("[data-price-total]").textContent = "—";
    message.textContent = `Enter weight to see price. Maximum ${maximumWeight} kg.`;
    return;
  }
  if (weight > maximumWeight) {
    preview.querySelector("[data-price-base]").textContent = "—";
    preview.querySelector("[data-price-weight]").textContent = "—";
    preview.querySelector("[data-price-total]").textContent = "—";
    message.textContent = `Weight exceeds the ${maximumWeight} kg limit.`;
    return;
  }
  const isValidWeight = Number.isFinite(weight) && weight > 0;
  const charge = isValidWeight ? weight * perKgRate : 0;

  message.textContent = "Estimated price only. Final price is confirmed after weighing.";
  preview.querySelector("[data-price-base]").textContent = formatCurrency(rate);
  preview.querySelector("[data-price-weight]").textContent = formatCurrency(charge);
  preview.querySelector("[data-price-total]").textContent = formatCurrency(rate + charge);
}

function formatPickupSlot(slot) {
  const [hourText] = slot.split(":");
  const hour = Number(hourText);
  const period = hour >= 12 ? "PM" : "AM";
  const twelveHour = hour % 12 || 12;
  return `${twelveHour}:00 ${period}`;
}

function updatePickupSlots(dateInput, timeSelect) {
  const date = dateInput.value;
  const previousValue = timeSelect.value;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = date ? "Choose a pickup time" : "Choose a date first";
  timeSelect.replaceChildren(placeholder);

  if (!date) {
    return;
  }

  const [year, month, day] = date.split("-").map(Number);
  const weekday = new Date(year, month - 1, day).getDay();
  const slotAttribute = weekday === 0 ? "sundaySlots" : "weekdaySlots";
  const slots = JSON.parse(timeSelect.dataset[slotAttribute] || "[]");
  const remainingSlots = date === timeSelect.dataset.today
    ? slots.filter((slot) => slot > (timeSelect.dataset.currentTime || "00:00"))
    : slots;
  for (const slot of remainingSlots) {
    const option = document.createElement("option");
    option.value = slot;
    option.textContent = formatPickupSlot(slot);
    timeSelect.append(option);
  }

  if (remainingSlots.includes(previousValue)) {
    timeSelect.value = previousValue;
  }
  if (!remainingSlots.length) {
    placeholder.textContent = "No pickup times remain today";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-password-toggle]").forEach((toggle) => {
    const passwordInput = document.getElementById(toggle.dataset.passwordToggle);
    if (!passwordInput) {
      return;
    }

    toggle.addEventListener("click", () => {
      const isVisible = passwordInput.type === "text";
      passwordInput.type = isVisible ? "password" : "text";
      toggle.setAttribute("aria-pressed", String(!isVisible));
      toggle.setAttribute("aria-label", isVisible ? "Show password" : "Hide password");
      toggle.innerHTML = `<i class="fa-regular ${isVisible ? "fa-eye" : "fa-eye-slash"}" aria-hidden="true"></i>`;
    });
  });

  document.querySelectorAll("[data-open-dialog]").forEach((button) => {
    button.addEventListener("click", () => {
      document.getElementById(button.dataset.openDialog)?.showModal();
    });
  });

  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });

  document.querySelectorAll(".flash-alert").forEach((alert) => {
    const dismiss = () => {
      alert.classList.add("flash-alert--dismissed");
      window.setTimeout(() => alert.remove(), 180);
    };
    alert.querySelector(".flash-dismiss")?.addEventListener("click", dismiss);
    window.setTimeout(dismiss, 5000);
  });

  document.querySelectorAll('form[action*="register"], form[action*="account/password"]').forEach((form) => {
    form.addEventListener("submit", (event) => {
      const password = form.querySelector('[name="password"], [name="new_password"]');
      const confirmation = form.querySelector('[name="confirm_password"]');
      if (password && confirmation && password.value !== confirmation.value) {
        event.preventDefault();
        confirmation.setCustomValidity("Passwords do not match.");
        confirmation.reportValidity();
      } else {
        confirmation?.setCustomValidity("");
      }
    });
  });

  document.querySelectorAll("[data-price-preview]").forEach((preview) => {
    const serviceSelect = document.getElementById(preview.dataset.rateSelect);
    const weightInput = document.getElementById(preview.dataset.weightInput);
    serviceSelect?.addEventListener("change", () => updatePricePreview(preview));
    weightInput?.addEventListener("input", () => updatePricePreview(preview));
    updatePricePreview(preview);
  });

  document.querySelectorAll('input[type="date"][name="pickup_date"]').forEach((dateInput) => {
    const timeSelect = dateInput.form?.querySelector('select[name="pickup_time"]');
    if (!timeSelect) {
      return;
    }
    dateInput.addEventListener("change", () => updatePickupSlots(dateInput, timeSelect));
    updatePickupSlots(dateInput, timeSelect);
  });
});
