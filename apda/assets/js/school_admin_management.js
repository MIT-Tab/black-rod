function initSchoolAdminManagement() {
  if (!document.getElementById("school-admin-management")) {
    return;
  }

  if (typeof window.jQuery === "undefined") {
    setTimeout(initSchoolAdminManagement, 50);
    return;
  }

  const $ = window.jQuery;
  const $container = $("#school-admin-management");

  function getListRole($list) {
    return ($list.data("role") || "admin").toString();
  }

  function getCurrentAdminId($list) {
    const value = $list.data("current-admin-id");
    if (!value) {
      return null;
    }
    const parsed = Number(value);
    return Number.isNaN(parsed) ? null : parsed;
  }

  function getSchoolId($list) {
    return $list.data("school-id");
  }

  function canMakePrimary($list, adminId) {
    if (!window.schoolAdminPrimaryUrl) {
      return false;
    }
    const role = getListRole($list);
    if (role === "superuser") {
      return true;
    }
    if (role === "primary") {
      return adminId !== getCurrentAdminId($list);
    }
    return false;
  }

  function canRemoveAdmin($list, adminId) {
    const role = getListRole($list);
    const currentAdminId = getCurrentAdminId($list);

    if (role === "superuser") {
      return true;
    }
    if (role === "primary") {
      return currentAdminId !== null && adminId !== currentAdminId;
    }
    if (role === "admin") {
      return currentAdminId !== null && adminId === currentAdminId;
    }
    return false;
  }

  function removeEmptyState($list) {
    $list.find("li.text-muted").remove();
  }

  function ensureEmptyState($list) {
    if ($list.find("li.admin-entry").length === 0) {
      $list.append('<li class="text-muted py-1"><em>No admins</em></li>');
    }
  }

  function renderActionButtons($list, $entry, isPrimary) {
    const $actions = $entry.find(".admin-actions");
    $actions.empty();
    const adminId = Number($entry.data("admin-id"));
    const schoolId = getSchoolId($list);
    const role = getListRole($list);
    const isSelf = adminId === getCurrentAdminId($list);

    if (!isPrimary && canMakePrimary($list, adminId)) {
      const $makePrimary = $(
        '<button type="button" class="btn btn-outline-primary btn-sm make-primary">Make Primary</button>',
      );
      $makePrimary.attr("data-school-id", schoolId);
      $makePrimary.attr("data-admin-id", adminId);
      $actions.append($makePrimary);
    }

    if (!isPrimary && canRemoveAdmin($list, adminId)) {
      const removeLabel =
        role === "admin" && isSelf ? "Remove Yourself" : "Remove";
      const $remove = $(
        '<button type="button" class="btn btn-danger btn-sm remove-admin"></button>',
      );
      $remove.attr("data-school-id", schoolId);
      $remove.attr("data-admin-id", adminId);
      $remove.text(removeLabel);
      if (role === "admin" && isSelf) {
        $remove.attr("data-self-remove", "true");
      }
      $actions.append($remove);
    }
  }

  function applyPrimaryBadge($entry) {
    if ($entry.find(".admin-primary-badge").length === 0) {
      $entry
        .find("strong")
        .after(
          '<span class="badge badge-primary badge-pill ml-2 admin-primary-badge">Primary</span>',
        );
    }
  }

  function removePrimaryBadge($entry) {
    $entry.find(".admin-primary-badge").remove();
  }

  function markAsPrimary($list, $entry) {
    if (!$entry || $entry.length === 0) {
      return;
    }
    applyPrimaryBadge($entry);
    renderActionButtons($list, $entry, true);
  }

  function markAsNonPrimary($list, $entry) {
    if (!$entry || $entry.length === 0) {
      return;
    }
    removePrimaryBadge($entry);
    renderActionButtons($list, $entry, false);
  }

  function buildAdminEntry($list, admin) {
    const $entry = $(
      '<li class="d-flex justify-content-between align-items-center py-1 admin-entry"></li>',
    );
    $entry.attr("data-admin-id", admin.id);
    $entry.attr("data-user-id", admin.user_id);

    const $details = $('<div class="d-flex align-items-center"></div>');
    $details.append(`<strong>${admin.username}</strong>`);
    if (admin.primary) {
      $details.append(
        '<span class="badge badge-primary badge-pill ml-2 admin-primary-badge">Primary</span>',
      );
    }
    if (admin.email) {
      $details.append(
        `<small class="text-muted ml-2">(${admin.email})</small>`,
      );
    }

    const $actions = $('<div class="btn-group btn-group-sm admin-actions" role="group"></div>');

    $entry.append($details);
    $entry.append($actions);

    renderActionButtons($list, $entry, Boolean(admin.primary));
    return $entry;
  }

  function handlePrimaryUpdate(response) {
    if (!response.success) {
      return;
    }

    const adminList = $(`#admin-list-${response.school_id}`);
    if (!adminList.length) {
      return;
    }

    const $newPrimary = adminList.find(
      `li[data-admin-id="${response.primary_admin_id}"]`,
    );
    markAsPrimary(adminList, $newPrimary);

    if (
      getListRole(adminList) !== "superuser" &&
      Number(response.primary_admin_id) === getCurrentAdminId(adminList)
    ) {
      adminList.data("role", "primary");
      adminList.attr("data-role", "primary");
    }

    if (response.demoted_admin_id) {
      const $demoted = adminList.find(
        `li[data-admin-id="${response.demoted_admin_id}"]`,
      );
      markAsNonPrimary(adminList, $demoted);

      if (
        Number(response.demoted_admin_id) === getCurrentAdminId(adminList) &&
        getListRole(adminList) === "primary"
      ) {
        adminList.data("role", "admin");
        adminList.attr("data-role", "admin");
      }
    } else {
      adminList
        .find("li.admin-entry")
        .not($newPrimary)
        .each((_, element) => {
          markAsNonPrimary(adminList, $(element));
        });
    }
  }

  $container.find(".user-autocomplete").select2({
    ajax: {
      url: window.userAutocompleteUrl,
      dataType: "json",
      delay: 250,
      data(params) {
        return {
          q: params.term,
        };
      },
      processResults(data) {
        return {
          results: data.results,
        };
      },
      cache: true,
    },
    minimumInputLength: 2,
    placeholder: "Search...",
    allowClear: true,
    width: "100%",
    dropdownAutoWidth: true,
  });

  $container.on("submit", ".add-admin-form", function handleAddAdminSubmit(e) {
    e.preventDefault();
    const form = $(this);
    const schoolId = form.data("school-id");
    const userId = form.find('select[name="user_id"]').val();

    if (!userId) {
      alert("Please select a user");
      return;
    }

    $.ajax({
      url: window.schoolAdminAddUrl,
      method: "POST",
      data: {
        school_id: schoolId,
        user_id: userId,
        csrfmiddlewaretoken: form.find('[name="csrfmiddlewaretoken"]').val(),
      },
      success(response) {
        if (response.success) {
          const adminList = $(`#admin-list-${schoolId}`);
          removeEmptyState(adminList);
          const newItem = buildAdminEntry(adminList, response.admin);
          adminList.append(newItem);
          form.find('select[name="user_id"]').val(null).trigger("change");
        }
      },
      error(xhr) {
        alert(
          `Error adding admin: ${
            xhr.responseJSON ? xhr.responseJSON.error : "Unknown error"
          }`,
        );
      },
    });
  });

  $container.on("click", ".remove-admin", function handleRemoveAdmin() {
    const button = $(this);
    const adminList = button.closest("ul");
    const adminId = Number(button.data("admin-id"));
    const isSelfRemoval = adminId === getCurrentAdminId(adminList);
    const confirmationMessage = isSelfRemoval
      ? "Removing yourself will revoke your school admin access. Continue?"
      : "Are you sure you want to remove this admin?";

    // eslint-disable-next-line no-alert, no-restricted-globals
    if (!confirm(confirmationMessage)) {
      return;
    }

    const entry = button.closest("li");

    $.ajax({
      url: window.schoolAdminRemoveUrl,
      method: "POST",
      data: {
        school_admin_id: adminId,
        csrfmiddlewaretoken: window.csrfToken,
      },
      success(response) {
        if (response.success) {
          entry.remove();
          ensureEmptyState(adminList);

          if (isSelfRemoval) {
            window.location.href = "/";
            return;
          }
        }
      },
      error(xhr) {
        alert(
          `Error removing admin: ${
            xhr.responseJSON ? xhr.responseJSON.error : "Unknown error"
          }`,
        );
      },
    });
  });

  $container.on("click", ".make-primary", function handleMakePrimary() {
    const button = $(this);
    const adminId = button.data("admin-id");
    const adminList = button.closest("ul");
    const adminName =
      button.closest(".admin-entry").find("strong").text().trim() || "this admin";
    const confirmMessage = `Are you sure you want to make ${adminName} the primary admin?`;

    // eslint-disable-next-line no-alert, no-restricted-globals
    if (!confirm(confirmMessage)) {
      return;
    }

    $.ajax({
      url: window.schoolAdminPrimaryUrl,
      method: "POST",
      data: {
        school_admin_id: adminId,
        csrfmiddlewaretoken: window.csrfToken,
      },
      success(response) {
        handlePrimaryUpdate(response);
      },
      error(xhr) {
        alert(
          `Error updating primary admin: ${
            xhr.responseJSON ? xhr.responseJSON.error : "Unknown error"
          }`,
        );
      },
    });
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initSchoolAdminManagement);
} else {
  initSchoolAdminManagement();
}
