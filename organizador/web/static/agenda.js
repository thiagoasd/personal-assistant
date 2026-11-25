// Filtros do calendário da Agenda: cada tipo da legenda esconde ou mostra seus itens.
// A escolha fica guardada só neste navegador.
(function () {
  "use strict";
  var KEY = "agenda-escondidos";
  var card = document.querySelector(".calendar");
  var buttons = document.querySelectorAll(".cal-filters button[data-kind]");
  if (!card || !buttons.length) return;
  var section = card.closest("section");

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (e) { return []; }
  }
  function save(hidden) {
    try { localStorage.setItem(KEY, JSON.stringify(hidden)); } catch (e) { /* sem armazenamento */ }
  }

  function apply(hidden) {
    buttons.forEach(function (button) {
      var kind = button.dataset.kind;
      var off = hidden.indexOf(kind) !== -1;
      button.setAttribute("aria-pressed", off ? "false" : "true");
      section.classList.toggle("hide-" + kind, off);
    });
    // Na lista do celular, some o dia que ficou sem nenhum item visível.
    var anyVisible = false;
    section.querySelectorAll(".cal-list > li:not(.muted)").forEach(function (day) {
      var visible = Array.prototype.some.call(day.querySelectorAll(".cal-entry"), function (entry) {
        return hidden.indexOf(entry.classList[1]) === -1;
      });
      day.hidden = !visible;
      anyVisible = anyVisible || visible;
    });
    var note = section.querySelector(".cal-all-hidden");
    var hasDays = section.querySelector(".cal-list > li:not(.muted)");
    if (note) note.hidden = !hasDays || anyVisible;
  }

  var hidden = load();
  apply(hidden);
  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      var kind = button.dataset.kind;
      var at = hidden.indexOf(kind);
      if (at === -1) hidden.push(kind); else hidden.splice(at, 1);
      save(hidden);
      apply(hidden);
    });
  });
})();
