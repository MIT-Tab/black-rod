import "bootstrap";

import "bootstrap-datepicker/build/build.less";
import "../css/app.scss";
import "../css/navigation.scss"

import "bootstrap-datepicker";

import "jquery-ui/ui/widgets/sortable";
import "jquery-ui/ui/widgets/mouse";
import "jquery-ui/ui/widget";

import "./sortable-formset";
import "./school_admin_management";
import "./data-entry";
import "./data-entry";
import "./replay";

function getCookie(name) {
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i += 1) {
      const cookie = cookies[i].trim();
      // Does this cookie string begin with the name we want?
      if (cookie.substring(0, name.length + 1) === `${name}=`) {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
}

function updateDeleteListeners() {
  $(".delete_button").click((event) => {
    event.preventDefault();

    $.post($(event.target).attr("href"), {
      csrfmiddlewaretoken: getCookie("csrftoken"),
    }).done(() => {
      $(event.target).parent().remove();
    });
  });
}

function teamSearch() {
  const input = document.getElementById("teams");
  const filter = input.value.toUpperCase();
  const ul = document.getElementById("team_list");
  const li = ul.getElementsByTagName("td");

  // Loop through all list items, and hide those who don't match the search query
  for (let i = 0; i < li.length; i += 1) {
    const [a] = li[i].getElementsByTagName("button");
    const txtValue = a.textContent || a.innerText;
    if (txtValue.toUpperCase().indexOf(filter) > -1) {
      li[i].style.display = "";
    } else {
      li[i].style.display = "none";
    }
  }
}

$(document).ready(() => {
  $("#expandAll").on("click", (e) => {
    e.preventDefault();
    $(".collapse").toggleClass("show");
  });

  $(".clickable").on("click", function clickHandler(e) {
    e.preventDefault();
    $($(this).data("newtarget")).toggleClass("show");
  });

  $("#create_debater").click((event) => {
    event.preventDefault();

    let toSerialize = $("#debater_create_form").serialize();
    toSerialize = `${toSerialize}&first_name=${$("#id_name").val().split(" ")[0]}`;
    toSerialize = `${toSerialize}&last_name=${$("#id_name").val().split(" ")[1]}`;

    // eslint-disable-next-line no-console
    console.log(toSerialize);

    $.post("/core/debaters/create", toSerialize).done((data) => {
      $("#new_debaters").append(
        `<li>${$("#id_name").val().split(" ")[0]} ${
          $("#id_name").val().split(" ")[1]
        } (${
          $("#select2-id_school-container").html().split("; ")[1]
        }) <a href="/core/debaters/${data}/delete" class="delete_button">Delete</a>`,
      );
      $("#id_name").val("");
      $("#id_school").empty().trigger("change");
      updateDeleteListeners();
    });
  });

  $("#id_date").datepicker({
    format: "mm/dd/yyyy",
    /* 'startDate': '-3d' */
  });

  $("#teams").on("keyup", teamSearch);
});
