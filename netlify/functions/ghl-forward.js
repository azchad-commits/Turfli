/* ------------------------------------------------------------------
   Turfli: forwards website form submissions to GoHighLevel.

   WHY THIS EXISTS
   The browser cannot post directly to GoHighLevel. A cross-site POST
   with a JSON content type requires a permission check first, and the
   form navigates to /thanks/ before that check can finish, so the
   request is silently dropped.

   This runs on Netlify's servers instead. The page posts here, to its
   own domain, which needs no permission check. This file then posts on
   to GoHighLevel server to server, the same way a webhook would.
------------------------------------------------------------------ */

const WEBHOOK_URL =
  "https://services.leadconnectorhq.com/hooks/f5CR8vY1MBaywM65vzUF/webhook-trigger/X1PSEWaAbc47Kfh3SmWn";

exports.handler = async (event) => {
  if (event.httpMethod !== "POST") {
    return { statusCode: 405, body: "Method not allowed" };
  }

  let body;
  try {
    body = JSON.parse(event.body || "{}");
  } catch (e) {
    return { statusCode: 400, body: "Bad JSON" };
  }

  // Record where it came from, for troubleshooting later.
  body.forwarded_at = new Date().toISOString();
  body.forwarded_by = "netlify-function";

  try {
    const res = await fetch(WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const text = await res.text();
    console.log("GHL responded", res.status, text.slice(0, 300));
    return {
      statusCode: 204,
      headers: { "Cache-Control": "no-store" },
      body: ""
    };
  } catch (err) {
    // Never fail loudly: the form has already submitted to Netlify,
    // so the lead is safe either way.
    console.error("Forward to GHL failed:", err && err.message);
    return { statusCode: 204, body: "" };
  }
};
