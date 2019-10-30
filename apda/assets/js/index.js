import "bootstrap";

import "../css/app.scss";

function getCookie(name) {
    var cookieValue = null;
    if (document.cookie && document.cookie != '') {
        var cookies = document.cookie.split(';');
        for (var i = 0; i < cookies.length; i++) {
            var cookie = cookies[i].trim();
            // Does this cookie string begin with the name we want?
            if (cookie.substring(0, name.length + 1) == (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

$(document).ready(() => {
  $('#create_debater').click((event) => {
    event.preventDefault();

    $.post('/core/debaters/create', $('#debater_create_form').serialize()).done((data) => {
      $('#new_debaters').append('<li>' + $('#id_first_name').val() + ' ' + $('#id_last_name').val() + ' (' + $('#select2-id_school-container').html().split('; ')[1] + ') <a href="/core/debaters/' + data + '/delete" class="delete_button">Delete</a>');
      $('#id_first_name').val('');
      $('#id_last_name').val('');
      $('#id_school').empty().trigger('change');
      update_delete_listeners();
    });
  });
});

function update_delete_listeners() {
  $('.delete_button').click((event) => {
    event.preventDefault();
    
    $.post($(event.target).attr('href'), {'csrfmiddlewaretoken': getCookie('csrftoken')}).done((data) => {
      $(event.target).parent().remove();
    });
  });
}
