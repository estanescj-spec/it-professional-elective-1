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
  const weight = Number(weightInput?.value || 0);
  const perKgRate = Number(preview.dataset.perKgRate || 0);
  const isValidWeight = Number.isFinite(weight) && weight > 0;
  const charge = isValidWeight ? weight * perKgRate : 0;

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
  for (const slot of slots) {
    const option = document.createElement("option");
    option.value = slot;
    option.textContent = formatPickupSlot(slot);
    timeSelect.append(option);
  }

  if (slots.includes(previousValue)) {
    timeSelect.value = previousValue;
  }
}

document.addEventListener("DOMContentLoaded", () => {
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
