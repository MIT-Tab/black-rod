import "bootstrap";

import "../css/app.scss";

$(document).ready(() => {
  $('#create_debater').click((event) => {
    event.preventDefault();

    $.post('/core/debaters/create', $('#debater_create_form').serialize()).done((data) => {
      $('#new_debaters').append('<li>' + $('#id_first_name').val() + ' ' + $('#id_last_name').val() + ' (' + $('#select2-id_school-container').html().split('; ')[1] + ')');
      $('#id_first_name').val('');
      $('#id_last_name').val('');
      $('#id_school').empty().trigger('change');
    });
  });
});
