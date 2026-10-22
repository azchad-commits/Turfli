/* ------------------------------------------------------------------
   Turfli: forwards website form submissions to GoHighLevel.

   WHY THIS EXISTS
   A browser cannot post straight to GoHighLevel. A cross-site POST with
   a JSON content type needs a permission check first, and the form
   navigates away to /thanks/ before that check can finish, so the
   request is dropped silently. This runs on Netlify's servers instead,
   where no such restriction applies.

   HOW TO CHECK IT IS WORKING
   Open this address in a browser:
       https://turfli.com/.netlify/functions/ghl-forward?selftest=1
   It sends a test submission to GoHighLevel and shows you exactly what
   came back. No other tools needed.

   Plain https is used rather than fetch so this works on every version
   of Node that Netlify might run.
------------------------------------------------------------------ */

const https = require("https");
const { URL } = require("url");

const WEBHOOK_URL =
  "https://services.leadconnectorhq.com/hooks/f5CR8vY1MBaywM65vzUF/webhook-trigger/X1PSEWaAbc47Kfh3SmWn";

function postToGHL(payload) {
  return new Promise((resolve) => {
    let data;
    try {
      data = JSON.stringify(payload);
    } catch (e) {
      return resolve({ ok: false, stage: "stringify", error: String(e) });
    }

    let u;
    try {
      u = new URL(WEBHOOK_URL);
    } catch (e) {
      return resolve({ ok: false, stage: "url", error: String(e) });
    }

    const req = https.request(
      {
        hostname: u.hostname,
        path: u.pathname + u.search,
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(data)
        },
        timeout: 8000
      },
      (res) => {
        let body = "";
        res.on("data", (c) => (body += c));
        res.on("end", () =>
          resolve({
            ok: res.statusCode >= 200 && res.statusCode < 300,
            stage: "complete",
            status: res.statusCode,
            response: body.slice(0, 500)
          })
        );
      }
    );

    req.on("timeout", () => {
      req.destroy();
      resolve({ ok: false, stage: "timeout", error: "GHL did not reply in 8s" });
    });
    req.on("error", (e) =>
      resolve({ ok: false, stage: "request", error: String(e && e.message) })
    );

    req.write(data);
    req.end();
  });
}

exports.handler = async (event) => {
  const json = (code, obj) => ({
    statusCode: code,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    body: JSON.stringify(obj, null, 2)
  });

  // ---- self test: open this in a browser to see what happens ----
  if (event.httpMethod === "GET") {
    if (!(event.queryStringParameters || {}).selftest) {
      return json(200, {
        status: "alive",
        hint: "Add ?selftest=1 to send a test submission to GoHighLevel."
      });
    }
    const now = new Date().toISOString();
    const result = await postToGHL({
      payload: {
        data: {
          "first-name": "Self",
          "last-name": "Test",
          first_name: "Self",
          firstName: "Self",
          name: "Self",
          phone: "+14805550199",
          city: "Scottsdale",
          notes: "Automatic self test from the website. Safe to delete.",
          message: "Automatic self test from the website. Safe to delete.",
          source: "function-selftest",
          form_name: "function-selftest",
          page_url: "https://turfli.com/.netlify/functions/ghl-forward",
          submitted_at: now
        },
        form_name: "function-selftest",
        site_url: "https://turfli.com",
        created_at: now
      }
    });
    return json(result.ok ? 200 : 502, {
      selftest: result.ok ? "PASSED" : "FAILED",
      sent_at: now,
      node_version: process.version,
      result
    });
  }

  if (event.httpMethod !== "POST") {
    return json(405, { error: "Method not allowed" });
  }

  let body;
  try {
    body = JSON.parse(event.body || "{}");
  } catch (e) {
    return json(400, { error: "Body was not valid JSON" });
  }

  body.forwarded_at = new Date().toISOString();
  body.forwarded_by = "netlify-function";

  const result = await postToGHL(body);

  // Logged so it appears in the Netlify function log.
  console.log("ghl-forward ->", JSON.stringify(result));

  // Always 200 so the browser never reports an error, but the body
  // carries the real outcome for anyone who looks.
  return json(200, { received: true, forwarded: result });
};
