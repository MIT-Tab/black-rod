function settleSummernoteLayout() {
  const jquery = window.jQuery;
  if (!jquery) return;

  jquery(document).on(
    "summernote.init",
    "textarea.summernoteinplacewidget",
    (event, { editor, editable, toolbar }) => {
      const { height } = jquery(event.target).data("summernote").options;
      // The widget initially measures its toolbar before adding its wrapper.
      // Measure once more after layout and fonts settle, then leave resizing to Summernote.
      document.fonts.ready.then(() => {
        requestAnimationFrame(() => {
          const chrome =
            toolbar.outerHeight() +
            editor.find(".note-status-output").outerHeight() +
            editor.find(".note-statusbar").outerHeight() +
            (editor.outerHeight() - editor.innerHeight());
          editable.outerHeight(Math.floor(height - chrome));
        });
      });
    },
  );
}

export default settleSummernoteLayout;
