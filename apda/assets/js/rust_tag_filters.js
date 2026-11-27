async function initTagFilters() {
  const fields = $("select[data-rust-tag-url], select[data-rust-select-url]");
  if (!fields.length) return;
  await Promise.all([
    import("select2"),
    import("select2/dist/css/select2.css"),
  ]);
  fields.each((_, field) => {
    const tagMode = !!field.dataset.rustTagUrl;
    const url = field.dataset.rustTagUrl || field.dataset.rustSelectUrl;
    $(field).select2({
      width: "100%",
      placeholder: field.dataset.rustSelectPlaceholder || "",
      minimumInputLength: Number(field.dataset.rustSelectMinimum || 0),
      tags: tagMode,
      tokenSeparators: tagMode ? [","] : undefined,
      allowClear: !field.multiple && !field.required,
      ajax: {
        url,
        dataType: "json",
        cache: true,
        delay: 250,
        data: (params) => {
          const forward = field.dataset.rustSelectForward;
          return {
            q: params.term || "",
            page: params.page || 1,
            ...(forward
              ? {
                  forward: JSON.stringify({
                    [forward]: field.form.elements[forward].value,
                  }),
                }
              : {}),
          };
        },
        processResults: (payload) => ({
          ...payload,
          results: payload.results.map((entry) => ({
            ...entry,
            id: tagMode && !entry.create_id ? entry.text : entry.id,
          })),
        }),
      },
    });
    if (field.hasAttribute("data-rust-tag-create")) {
      $(field).on("select2:selecting", (event) => {
        const item = event.params.args.data;
        if (!item.create_id) return;
        event.preventDefault();
        const token = field.form.elements.csrfmiddlewaretoken.value;
        $.post(url, { text: item.id, csrfmiddlewaretoken: token }).done(
          (created) => {
            const option = document.createElement("option");
            option.text = created.text;
            option.value = created.text;
            option.selected = true;
            $(field).append(option).trigger("change");
          },
        );
      });
    }
  });
}

export default initTagFilters;
