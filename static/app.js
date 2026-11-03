(function () {
  const map = L.map("map", { zoomControl: true });
  const tileUrl = document.querySelector("meta[name=tile-url]").content;
  if (tileUrl) L.tileLayer(tileUrl, { maxZoom: 19, attribution: "OpenStreetMap-Mitwirkende" }).addTo(map);

  const FARBEN = { leer: "#E8731A", teil: "#E9B949", voll: "#9AA3AD", mein: "#1F2430" };
  const AUSWAHL = "#1766D6";
  // Canvas mit großer Toleranz: Linien lassen sich auch mit dem Finger gut treffen
  const renderer = L.canvas({ tolerance: 12 });
  const kannHover = window.matchMedia("(hover: hover)").matches;
  const layerGroup = L.featureGroup().addTo(map);
  const labelGroup = L.featureGroup().addTo(map);

  let daten = null;          // {bezirke, eigene}
  let auswahlId = null;      // vorgemerkter Bezirk (1. Antippen)
  let formularOffen = false; // Formular für auswahlId offen (2. Antippen)
  let hoverId = null;
  let layers = {}, labels = {};
  let ersterAufbau = true;

  const $ = (s) => document.querySelector(s);
  const uebersicht = $("#uebersicht"), detail = $("#detail"), eigene = $("#eigene"), leiste = $("#auswahl-leiste");

  function el(tag, attrs = {}, ...kinder) {
    const e = document.createElement(tag);
    Object.entries(attrs).forEach(([k, v]) => (k === "class" ? (e.className = v) : e.setAttribute(k, v)));
    kinder.forEach((k) => e.append(k));
    return e;
  }

  const bezirk = (id) => daten.bezirke.features.find((f) => f.properties.id === id)?.properties;

  function klasse(p, eigeneIds) {
    if (eigeneIds.has(p.id)) return "mein";
    if (p.anzahl === 0 && !p.gesperrt) return "leer";
    if (p.waehlbar) return "teil";
    return "voll";
  }

  const plaetze = (n) => (n === 1 ? "1 Platz" : `${n} Plätze`);

  function statusText(p) {
    if (p.gesperrt) return "Gesperrt";
    if (p.anzahl === 0) return `Noch niemand angemeldet – ${plaetze(p.max_effektiv)} frei`;
    if (p.frei > 0) return `${p.anzahl} von ${plaetze(p.max_effektiv)} belegt – noch ${p.frei} frei`;
    return `Voll (${p.anzahl} von ${plaetze(p.max_effektiv)} belegt)`;
  }

  async function laden() {
    const r = await fetch("/api/bezirke", { credentials: "same-origin" });
    if (r.status === 403) { location.reload(); return; }
    daten = await r.json();
    if (auswahlId && !bezirk(auswahlId)) { auswahlId = null; formularOffen = false; }
    zeichnen();
  }

  // ---------- Aufbau ----------
  function zeichnen() {
    layerGroup.clearLayers(); labelGroup.clearLayers(); layers = {}; labels = {};
    const eigeneIds = new Set(daten.eigene.map((e) => e.bezirk_id));

    daten.bezirke.features.forEach((f) => {
      const p = f.properties;
      p.klasse = klasse(p, eigeneIds);
      const layer = L.geoJSON(f, { renderer, style: { lineCap: "round", lineJoin: "round" } })
        .on("click", () => tippen(p.id));
      if (kannHover) {
        layer.bindTooltip(p.name, { sticky: true, direction: "top", offset: [0, -8] })
          .on("mouseover", () => hover(p.id))
          .on("mouseout", () => hover(null));
      }
      layer.addTo(layerGroup); layers[p.id] = layer;

      if (p.center) {
        labels[p.id] = L.marker([p.center[1], p.center[0]], {
          icon: L.divIcon({ className: "label label-" + p.klasse, html: `<span>${p.anzahl}/${p.max_effektiv}</span>`, iconSize: [40, 22] }),
          keyboard: false,
        }).on("click", () => tippen(p.id)).addTo(labelGroup);
      }
    });

    if (ersterAufbau && layerGroup.getLayers().length) {
      map.fitBounds(layerGroup.getBounds().pad(0.05));
      ersterAufbau = false;
    }

    listeZeichnen();
    eigeneZeichnen();
    ansichtAktualisieren();
  }

  function listeZeichnen() {
    const ul = $("#bezirk-liste"); ul.replaceChildren();
    daten.bezirke.features.forEach(({ properties: p }) => {
      const kopf = el("button", { type: "button", class: "kopf", "aria-expanded": "false" },
        el("i", { class: `l l-${p.klasse}` }), el("span", { class: "n" }, p.name),
        el("span", { class: "z" }, `${p.anzahl}/${p.max_effektiv}`));
      kopf.addEventListener("click", () => tippen(p.id, "liste"));
      if (kannHover) {
        kopf.addEventListener("mouseenter", () => hover(p.id));
        kopf.addEventListener("mouseleave", () => hover(null));
      }

      const aktion = p.waehlbar
        ? el("button", { type: "button", class: "primaer" }, "Für diesen Bezirk anmelden")
        : el("p", { class: "voll-hinweis" }, "Dieser Bezirk ist bereits voll.");
      if (p.waehlbar) aktion.addEventListener("click", formularOeffnen);
      const mehr = el("div", { class: "mehr" },
        el("p", { class: "status" }, statusText(p)),
        el("p", { class: "strassen-kurz" }, p.strassen.join(", ")),
        aktion);

      ul.append(el("li", { "data-id": p.id }, kopf, mehr));
    });
  }

  function eigeneZeichnen() {
    eigene.hidden = daten.eigene.length === 0;
    const ul = $("#eigene-liste"); ul.replaceChildren();
    daten.eigene.forEach((e) => {
      const b = bezirk(e.bezirk_id);
      const fest = e.status === "bestaetigt";
      const text = el("span", {}, el("strong", {}, b ? b.name : e.bezirk_id), " ",
        el("span", { class: fest ? "badge badge-ok" : "badge badge-offen" }, fest ? "fest zugeteilt" : "wartet auf Bestätigung"),
        el("br"), el("small", {}, e.personen.map((p) => `${p.name} (${p.klasse})`).join(", ")));
      const li = el("li", {}, text);
      if (!fest) {
        const zurueckziehen = el("button", { type: "button", class: "leise" }, "zurückziehen");
        zurueckziehen.addEventListener("click", async () => {
          if (!confirm("Möchten Sie diese Anmeldung wirklich zurückziehen?")) return;
          const r = await fetch("/api/anmeldung/" + e.edit_token, { method: "DELETE", headers: {"X-CSRF-Token": document.querySelector("meta[name=csrf-token]").content}, credentials: "same-origin" });
          if (!r.ok) alert((await r.json().catch(() => ({}))).fehler || "Das hat leider nicht geklappt.");
          $("#danke").hidden = true;
          laden();
        });
        li.append(zurueckziehen);
      }
      ul.append(li);
    });
  }

  // ---------- Zustand → Darstellung ----------
  function stil(p) {
    if (p.id === auswahlId) return { color: AUSWAHL, weight: 8, opacity: 0.6 };
    if (p.id === hoverId) return { color: AUSWAHL, weight: 8, opacity: 0.55 };
    return { color: FARBEN[p.klasse], weight: 6, opacity: auswahlId ? 0.3 : 0.55 };
  }

  function stileAktualisieren() {
    daten.bezirke.features.forEach(({ properties: p }) => {
      layers[p.id]?.setStyle(stil(p));
      labels[p.id]?.getElement()?.classList.toggle("label-auswahl", p.id === auswahlId || p.id === hoverId);
    });
    [auswahlId, hoverId].forEach((id) => layers[id]?.bringToFront());
    document.querySelectorAll("#bezirk-liste li").forEach((li) => {
      const an = li.dataset.id === auswahlId;
      li.classList.toggle("auswahl", an);
      li.classList.toggle("hover", li.dataset.id === hoverId);
      li.querySelector(".kopf").setAttribute("aria-expanded", an);
    });
  }

  function ansichtAktualisieren() {
    const p = auswahlId && bezirk(auswahlId);
    stileAktualisieren();

    leiste.hidden = !p || formularOffen;
    if (p) {
      $("#al-name").textContent = p.name;
      $("#al-status").textContent = statusText(p);
      $("#al-eintragen").hidden = !p.waehlbar;
    }

    uebersicht.hidden = formularOffen;
    detail.hidden = !formularOffen;
    if (formularOffen && p) {
      $("#d-name").textContent = p.name;
      $("#d-status").textContent = statusText(p);
      const us = $("#d-strassen"); us.replaceChildren(...p.strassen.map((s) => el("li", {}, s)));
      $("#formular").hidden = !p.waehlbar;
      $("#d-voll").hidden = p.waehlbar;
    }
  }

  // ---------- Bedienung ----------
  function hover(id) {
    hoverId = id;
    if (daten) stileAktualisieren();
  }

  function tippen(id, quelle) {
    if (id === auswahlId) {
      if (!formularOffen) formularOeffnen();
      return;
    }
    auswahlId = id; formularOffen = false;
    $("#f-fehler").textContent = "";
    ansichtAktualisieren();
    const l = layers[id];
    if (l) map.flyToBounds(l.getBounds(), { paddingTopLeft: [30, 30], paddingBottomRight: [30, 90], maxZoom: 17, duration: 0.6 });
    if (quelle !== "liste") document.querySelector(`#bezirk-liste li[data-id="${id}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function formularOeffnen() {
    if (!auswahlId) return;
    formularOffen = true;
    ansichtAktualisieren();
    $("#panel").scrollTop = 0;
  }

  function auswahlAufheben() {
    auswahlId = null; formularOffen = false;
    ansichtAktualisieren();
  }

  $("#al-eintragen").addEventListener("click", formularOeffnen);
  $("#al-schliessen").addEventListener("click", auswahlAufheben);

  $("#zurueck").addEventListener("click", () => {
    formularOffen = false;
    ansichtAktualisieren();
    document.querySelector(`#bezirk-liste li[data-id="${auswahlId}"]`)?.scrollIntoView({ block: "nearest" });
  });

  // ---------- Personen im Formular ----------
  const formular = $("#formular"), personenBox = $("#personen");
  const MAX_PERSONEN = Number(formular.dataset.max) || 3;
  const AUTOCOMPLETE = { name: "name", telefon: "tel", email: "email" };

  function personenNummerieren() {
    const alle = [...personenBox.querySelectorAll(".person")];
    alle.forEach((fs, i) => {
      const nr = i + 1;
      fs.querySelector(".p-titel").textContent = alle.length > 1 ? `Person ${nr}` : "Ihre Angaben";
      fs.querySelector(".p-klasse").textContent = nr === 1 ? "Klasse Ihres Kindes" : "Klasse des Kindes";
      fs.querySelector(".p-entfernen").hidden = nr === 1;
      fs.querySelectorAll("input").forEach((inp) => {
        inp.name = `p${nr}_${inp.dataset.feld}`;
        // Nur Person 1 automatisch mit den eigenen Daten ausfüllen lassen
        inp.autocomplete = nr === 1 ? (AUTOCOMPLETE[inp.dataset.feld] || "off") : "off";
      });
    });
    $("#person-plus").hidden = alle.length >= MAX_PERSONEN;
  }

  function personHinzufuegen(fokus) {
    const fs = $("#person-vorlage").content.firstElementChild.cloneNode(true);
    fs.querySelector(".p-entfernen").addEventListener("click", () => { fs.remove(); personenNummerieren(); });
    personenBox.append(fs);
    personenNummerieren();
    if (fokus) fs.querySelector("input").focus();
  }

  function personenZuruecksetzen() {
    personenBox.replaceChildren();
    personHinzufuegen(false);
  }

  $("#person-plus").addEventListener("click", () => personHinzufuegen(true));
  personenZuruecksetzen();

  formular.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const personen = [...personenBox.querySelectorAll(".person")].map((fs) =>
      Object.fromEntries([...fs.querySelectorAll("input")].map((inp) => [inp.dataset.feld, inp.value.trim()])));
    const body = { bezirk_id: auswahlId, personen };
    const btn = ev.target.querySelector("button[type=submit]"); btn.disabled = true;
    try {
      const r = await fetch("/api/anmeldung", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector("meta[name=csrf-token]").content }, body: JSON.stringify(body),
      });
      const j = await r.json();
      if (!r.ok) { $("#f-fehler").textContent = j.fehler || "Das hat leider nicht geklappt."; laden(); return; }
    } catch {
      $("#f-fehler").textContent = "Keine Verbindung. Bitte versuchen Sie es noch einmal.";
      return;
    } finally {
      btn.disabled = false;
    }
    personenZuruecksetzen();
    $("#f-fehler").textContent = "";
    auswahlId = null; formularOffen = false;
    await laden();
    $("#danke").hidden = false;
    eigene.scrollIntoView({ block: "start", behavior: "smooth" });
  });

  laden();
  setInterval(laden, 30000); // Zähler aktuell halten
})();
