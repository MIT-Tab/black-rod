import initializeDropdowns from "./dropdowns";

const targetFor = (id) => document.getElementById(id);
const collapseSelector = "[data-ui-collapse]";
const tabSelector = "[data-ui-tab]";

function setExpanded(trigger, expanded) {
  const panel = targetFor(trigger.dataset.uiCollapse);
  panel.classList.toggle("show", expanded);
  trigger.setAttribute("aria-expanded", String(expanded));
}

function initializeDisclosures() {
  const triggers = Array.from(document.querySelectorAll(collapseSelector));
  triggers.forEach((trigger) => {
    const panel = targetFor(trigger.dataset.uiCollapse);
    if (!panel) return;
    trigger.setAttribute("aria-controls", panel.id);
    trigger.setAttribute(
      "aria-expanded",
      String(panel.classList.contains("show")),
    );
    trigger.addEventListener("click", () => {
      setExpanded(trigger, !panel.classList.contains("show"));
    });
  });

  const expandAll = targetFor("expandAll");
  if (expandAll) {
    const contributions = triggers.filter((trigger) =>
      trigger.closest("#coty"),
    );
    expandAll.addEventListener("click", (event) => {
      event.preventDefault();
      const expanded = contributions.some(
        (trigger) => trigger.getAttribute("aria-expanded") === "false",
      );
      contributions.forEach((trigger) => setExpanded(trigger, expanded));
    });
  }
}

function initializeTabs() {
  document.querySelectorAll('[role="tablist"]').forEach((list) => {
    const tabs = Array.from(list.querySelectorAll(tabSelector)).filter((tab) =>
      targetFor(tab.hash.slice(1)),
    );
    const select = (selected) => {
      tabs.forEach((tab) => {
        const panel = targetFor(tab.hash.slice(1));
        const active = selected === tab;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
        tab.setAttribute("tabindex", active ? "0" : "-1");
        panel.classList.toggle("active", active);
        panel.classList.toggle("show", active);
      });
    };

    tabs.forEach((tab, index) => {
      const panel = targetFor(tab.hash.slice(1));
      if (!tab.id) tab.setAttribute("id", `${panel.id}-tab`);
      tab.setAttribute("aria-controls", panel.id);
      tab.setAttribute("role", "tab");
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", tab.id);
      tab.addEventListener("click", (event) => {
        event.preventDefault();
        select(tab);
      });
      tab.addEventListener("keydown", (event) => {
        const destinations = {
          ArrowLeft: (index + tabs.length - 1) % tabs.length,
          ArrowRight: (index + 1) % tabs.length,
          Home: 0,
          End: tabs.length - 1,
          " ": index,
        };
        const next = tabs[destinations[event.key]];
        if (!next) return;
        event.preventDefault();
        select(next);
        next.focus();
      });
    });

    const selected =
      tabs.find((tab) =>
        targetFor(tab.hash.slice(1)).classList.contains("active"),
      ) ||
      tabs.find((tab) => tab.classList.contains("active")) ||
      tabs[0];
    if (selected) select(selected);
  });
}

function initializeDialogs() {
  document.querySelectorAll("dialog.modal").forEach((dialog) => {
    dialog.addEventListener("click", (event) => {
      if (
        event.target === dialog ||
        event.target.closest("[data-ui-dialog-close]")
      ) {
        dialog.close();
      }
    });
  });

  document.querySelectorAll("[data-ui-dialog]").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      targetFor(trigger.dataset.uiDialog)?.showModal();
    });
  });
}

function initializeTooltips() {
  document.querySelectorAll("[data-ui-tooltip]").forEach((trigger, index) => {
    const tooltip = document.createElement("span");
    tooltip.setAttribute("role", "tooltip");
    tooltip.id = `ui-tooltip-${index}`;
    tooltip.className = "ui-tooltip";
    tooltip.hidden = true;
    tooltip.textContent = trigger.title;
    trigger.removeAttribute("title");
    trigger.setAttribute("aria-describedby", tooltip.id);
    document.body.append(tooltip);

    const hide = () => {
      tooltip.hidden = true;
    };
    const show = () => {
      const anchor = trigger.getBoundingClientRect();
      tooltip.hidden = false;
      const left = Math.max(
        8,
        Math.min(
          anchor.left + (anchor.width - tooltip.offsetWidth) / 2,
          document.documentElement.clientWidth - tooltip.offsetWidth - 8,
        ),
      );
      const below =
        trigger.dataset.uiTooltip === "bottom" ||
        anchor.top < tooltip.offsetHeight + 8;
      tooltip.style.left = `${left}px`;
      tooltip.style.top = below
        ? `${anchor.bottom + 5}px`
        : `${anchor.top - tooltip.offsetHeight - 5}px`;
    };

    trigger.addEventListener("mouseenter", show);
    trigger.addEventListener("focus", show);
    trigger.addEventListener("click", show);
    trigger.addEventListener("blur", hide);
    trigger.addEventListener("mouseleave", () => {
      if (document.activeElement !== trigger) hide();
    });
    trigger.addEventListener("keydown", (event) => {
      if (event.key === "Escape") hide();
    });
    document.addEventListener(
      "click",
      (event) => {
        if (!trigger.contains(event.target)) hide();
      },
      true,
    );
    document.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initializeDropdowns();
  initializeDisclosures();
  initializeTabs();
  initializeDialogs();
  initializeTooltips();
  document.addEventListener("click", (event) => {
    event.target.closest("[data-ui-alert-close]")?.closest(".alert")?.remove();
  });
});
