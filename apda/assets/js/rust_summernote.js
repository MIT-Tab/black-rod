async function initRichTextEditors() {
  const fields = document.querySelectorAll("textarea[data-rust-summernote]");
  if (!fields.length) return;
  await Promise.all([
    import("summernote/dist/summernote-lite.min"),
    import("summernote/dist/summernote-lite.css"),
  ]);
  fields.forEach((field) => {
    const editor = $(field);
    const synchronize = () => {
      const content = editor.summernote("code");
      editor.val(
        ["<p><br></p>", "<p><br/></p>"].includes(content) ? "" : content,
      );
    };
    editor.summernote({
      width: "100%",
      height: 480,
      lang: "en-US",
      toolbar: [
        ["style", ["style"]],
        [
          "font",
          [
            "bold",
            "italic",
            "underline",
            "superscript",
            "subscript",
            "strikethrough",
            "clear",
          ],
        ],
        ["fontname", ["fontname"]],
        ["fontsize", ["fontsize"]],
        ["color", ["color"]],
        ["para", ["ul", "ol", "paragraph"]],
        ["height", ["height"]],
        ["table", ["table"]],
        ["insert", ["link", "picture", "video", "hr"]],
        ["view", ["fullscreen", "codeview"]],
        ["help", ["help"]],
      ],
      callbacks: {
        onInit: () => {
          const frame = editor.next(".note-editor");
          frame.wrap('<div class="summernote-div"></div>');
          const chrome = [
            ".note-toolbar",
            ".note-status-output",
            ".note-statusbar",
          ].reduce(
            (height, selector) =>
              height + (frame.find(selector).outerHeight() || 0),
            0,
          );
          frame
            .find(".note-editable")
            .outerHeight(
              480 - chrome - (frame.outerHeight() - frame.innerHeight()),
            );
        },
        onChange: synchronize,
        onBlur: synchronize,
        onBlurCodeview: synchronize,
        onImageUpload: async (files) => {
          const errorMessage = "Got an error while uploading images.";
          try {
            const form = new FormData();
            form.set(
              "csrfmiddlewaretoken",
              field.form.elements.csrfmiddlewaretoken.value,
            );
            Array.from(files).forEach((file) => form.append("files", file));
            const response = await fetch("/summernote/upload_attachment/", {
              method: "POST",
              body: form,
            });
            const data = await response.json();
            if (!response.ok) {
              window.alert(data.message || errorMessage);
              return;
            }
            data.files.forEach((file) =>
              editor.summernote("insertImage", file.url),
            );
          } catch {
            window.alert(errorMessage);
          }
        },
      },
    });
    field.form.addEventListener("submit", synchronize);
  });
}

export default initRichTextEditors;
