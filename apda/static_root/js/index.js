import "bootstrap";

import "../css/app.scss";

$(document).ready(() => {
  $('#create_debater').click((event) => {
    event.preventDefault();

    $.post('/core/debaters/create', $('#debater_create_form').serialize()).done((data) => {
      $('#new_debaters').append('<li>' + $('#id_first_name').val() + ' ' + $('#id_last_name').val() + ' (' + $('#select2-id_school-container').html().split('; ')[1] + ')'/* <a href="" data-url="/core/debaters/' + $('#id_school').val() + '/delete" class="delete_button">Delete</a>'*/);
      $('#id_first_name').val('');
      $('#id_last_name').val('');
      $('#id_school').empty().trigger('change');
      update_delete_listeners();
    });
  });
});

// THIS DOESN'T WORK
/*function update_delete_listeners() {
  $('.delete_button').each((button) => {
    $(button).click((event) => {
      event.preventDefault();

      $.post(button.data('url')).done((data) => {
	button.parent().remove();
      });
    });
  });
}*/
