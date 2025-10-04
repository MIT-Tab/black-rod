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
          const emptyItem = adminList.find("li em");
          if (emptyItem.length) {
            emptyItem.parent().remove();
          }

          const newItem = $(
            `<li class="d-flex justify-content-between align-items-center py-1" data-admin-id="${
              response.admin.id
            }">` +
              `<div><strong>${response.admin.username}</strong> ${
                response.admin.email
                  ? `<small class="text-muted">(${
                      response.admin.email
                    })</small>`
                  : ""
              }</div>` +
              `<button class="btn btn-xs btn-danger btn-sm remove-admin" data-school-id="${
                schoolId
              }" data-admin-id="${
                response.admin.id
              }" style="padding: 0px 6px; font-size: 14px;">×</button>` +
              `</li>`,
          );

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
    // eslint-disable-next-line no-alert, no-restricted-globals
    if (!confirm("Are you sure you want to remove this admin?")) {
      return;
    }

    const button = $(this);
    const adminId = button.data("admin-id");
    const listItem = button.closest("li");
    const adminList = button.closest("ul");

    $.ajax({
      url: window.schoolAdminRemoveUrl,
      method: "POST",
      data: {
        school_admin_id: adminId,
        csrfmiddlewaretoken: window.csrfToken,
      },
      success(response) {
        if (response.success) {
          listItem.remove();

          if (adminList.find("li").length === 0) {
            adminList.append(
              '<li class="text-muted py-1"><em>No admins</em></li>',
            );
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
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initSchoolAdminManagement);
} else {
  initSchoolAdminManagement();
}
