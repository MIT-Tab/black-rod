import "bootstrap";

import "../css/app.scss";

$(document).ready(() => {
  $('#create_debater').click((event) => {
    event.preventDefault();

    $.post('/core/debaters/create', $('#debater_create_form').serialize()).done((data) => {
      alert('Debater created!');
    });
  });
});
