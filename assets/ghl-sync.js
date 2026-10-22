/* ------------------------------------------------------------------
   Turfli: send website form submissions to GoHighLevel.

   HOW IT WORKS
   The form still submits to Netlify exactly as before. This script
   additionally posts a copy to the GoHighLevel inbound webhook. If
   GoHighLevel is unreachable the form still works and Netlify still
   stores the submission, so a lead can never be lost because of this.

   PAYLOAD SHAPE
   The existing GoHighLevel workflow reads fields as
       inboundWebhookRequest.payload.data.<field>
   which is the shape Netlify's own outgoing webhook sends. This script
   sends the same shape so the workflow needs no rebuilding.

   The same fields are also repeated at the top level, and common name
   variants are included as aliases, so the workflow keeps working
   whichever reference style its actions happen to use.
------------------------------------------------------------------ */
(function () {
  "use strict";

  var WEBHOOK_URL = "https://services.leadconnectorhq.com/hooks/f5CR8vY1MBaywM65vzUF/webhook-trigger/X1PSEWaAbc47Kfh3SmWn";

  if (!WEBHOOK_URL) return;

  // field -> extra keys carrying the same value, so the workflow can
  // reference whichever spelling it was built against.
  var ALIASES = {
    "first-name": ["first_name", "firstName", "name"],
    "last-name":  ["last_name", "lastName"],
    "notes":      ["message"],
    "project-type": ["project_type", "service"]
  };

  function sourceFallback() {
    var p = (location.pathname || "/").replace(/^\/|\/$/g, "");
    return p ? "site-" + p.replace(/\//g, "-") : "site-home";
  }

  function collect(form) {
    var data = {};

    new FormData(form).forEach(function (value, key) {
      if (key === "form-name") return;
      if (typeof value !== "string") return;
      value = value.trim();
      if (!value) return;
      data[key] = value;
    });

    // add aliases
    Object.keys(ALIASES).forEach(function (canonical) {
      if (!(canonical in data)) return;
      ALIASES[canonical].forEach(function (alias) {
        if (!(alias in data)) data[alias] = data[canonical];
      });
    });

    data.form_name = form.getAttribute("name") || "unknown";
    data["form-name"] = data.form_name;
    if (!data.source) data.source = sourceFallback();
    data.page_url = location.href;
    data.page_title = document.title;
    data.submitted_at = new Date().toISOString();

    // Netlify-shaped body, with a flat copy alongside it.
    var body = {
      payload: {
        data: data,
        form_name: data.form_name,
        site_url: location.origin,
        created_at: data.submitted_at
      }
    };
    Object.keys(data).forEach(function (k) {
      if (!(k in body)) body[k] = data[k];
    });

    return body;
  }

  function send(form) {
    var body;
    try {
      body = collect(form);
    } catch (e) {
      return; // never block the submission
    }

    try {
      fetch(WEBHOOK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        // keepalive lets the request finish after the browser has
        // already navigated to /thanks/.
        keepalive: true,
        mode: "cors"
      }).catch(function () { /* silent: Netlify still holds the lead */ });
    } catch (e) { /* silent */ }
  }

  document.addEventListener(
    "submit",
    function (e) {
      var form = e.target;
      if (!form || form.tagName !== "FORM") return;
      if (!form.hasAttribute("netlify") && !form.hasAttribute("data-netlify")) return;
      send(form);
      // No preventDefault: the normal Netlify submission continues.
    },
    true
  );
})();
