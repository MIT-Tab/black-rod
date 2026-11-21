(() => {
  const root = document.documentElement;
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  // Django admin uses the same storage key and values for its theme toggle.
  const storageKey = "theme";
  let storage;
  try {
    storage = window.localStorage;
  } catch {
    // The theme can still follow the system with browser storage disabled.
  }

  function readPreference(fallback = "auto") {
    try {
      return storage?.getItem(storageKey) || "auto";
    } catch {
      return fallback;
    }
  }

  let preference = "auto";
  let picker;

  function applyTheme(mode) {
    preference = mode === "light" || mode === "dark" ? mode : "auto";
    if (picker) picker.value = preference;
    const systemTheme = system.matches ? "dark" : "light";
    const theme = preference === "auto" ? systemTheme : preference;
    if (root.dataset.theme !== theme) {
      root.dataset.theme = theme;
      document.dispatchEvent(new Event("standings:themechange"));
    }
  }

  applyTheme(readPreference());
  system.addEventListener("change", () => applyTheme(preference));
  window.addEventListener("storage", (event) => {
    if (
      event.storageArea === storage &&
      (event.key === storageKey || event.key === null)
    ) {
      applyTheme(readPreference(preference));
    }
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) applyTheme(readPreference(preference));
  });

  document.addEventListener("DOMContentLoaded", () => {
    picker = document.getElementById("appearance-theme");
    if (!picker) return;
    picker.value = preference;
    picker.hidden = false;
    picker.addEventListener("change", () => {
      applyTheme(picker.value);
      try {
        storage?.setItem(storageKey, preference);
      } catch {
        // Keep the selection active for this page when it cannot be saved.
      }
    });
  });
})();
