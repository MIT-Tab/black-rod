import "bootstrap";

import "../../../node_modules/bootstrap-datepicker/build/build.less"
import "../css/app.scss";

import "bootstrap-datepicker";

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
  $('#expandAll').on('click', function(e) {
    e.preventDefault();
    $('.collapse').toggleClass('show');
  });

  $('.clickable').on('click', function(e) {
    e.preventDefault();
    $($(this).data('newtarget')).toggleClass('show');
  });
  
  $('#create_debater').click((event) => {
    event.preventDefault();

    var to_serialize = $('#debater_create_form').serialize()
    to_serialize = to_serialize + "&first_name=" + $('#id_name').val().split(' ')[0];
    to_serialize = to_serialize + "&last_name=" + $('#id_name').val().split(' ')[1];

    console.log(to_serialize);

    $.post('/core/debaters/create', to_serialize).done((data) => {
      $('#new_debaters').append('<li>' + $('#id_name').val().split(' ')[0] + ' ' + $('#id_name').val().split(' ')[1] + ' (' + $('#select2-id_school-container').html().split('; ')[1] + ') <a href="/core/debaters/' + data + '/delete" class="delete_button">Delete</a>');
      $('#id_name').val('');
      $('#id_school').empty().trigger('change');
      update_delete_listeners();
    });
  });

  $('#id_date').datepicker({
    'format': 'mm/dd/yyyy',
    'startDate': '-3d'

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

