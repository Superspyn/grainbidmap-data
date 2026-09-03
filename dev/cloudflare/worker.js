/**
 * Private relay for truck positions.
 *
 * Deere's API sends no CORS headers, so the map can never call it directly,
 * and the positions must not be public - they say when the yard is empty and
 * where equipment sits overnight. This Worker sits between: the farm PC PUTs
 * the latest positions, the password-protected page GETs them.
 *
 * Two separate tokens, because the two sides need different trust:
 *   PUSH_TOKEN  - only the farm PC has it. Write access.
 *   READ_TOKEN  - embedded in the private page. Read access only.
 *
 * The read token is visible to anyone who can view the private page's source.
 * That is the same set of people who can already see the positions on it, so
 * it gives nothing away - but it does mean the read token must never be put
 * on a public page.
 *
 * Deploy:
 *   1. Cloudflare dashboard -> Workers & Pages -> Create -> Worker
 *   2. Paste this in, Deploy
 *   3. Settings -> Variables -> add secrets PUSH_TOKEN and READ_TOKEN
 *   4. Settings -> Bindings -> KV namespace, variable name FLEET
 */

const KEY = "fleet";

// The private page is on a different origin, so the browser preflights.
function cors(origin) {
  return {
    "Access-Control-Allow-Origin": origin || "*",
    "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function bearer(request) {
  const header = request.headers.get("Authorization") || "";
  return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
}

// Compare in constant time. Overkill for a farm map, but a token check that
// leaks its answer through timing is a bad habit to write down.
function sameToken(a, b) {
  if (!a || !b || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin");
    const headers = cors(origin);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers });
    }

    if (request.method === "PUT") {
      if (!sameToken(bearer(request), env.PUSH_TOKEN)) {
        return new Response("forbidden", { status: 403, headers });
      }
      let body;
      try {
        body = await request.json();
      } catch (e) {
        return new Response("expected JSON", { status: 400, headers });
      }
      // Stamp arrival so the page can show how old the relay's copy is,
      // separately from how old each position is.
      body.relayed_at = new Date().toISOString();
      await env.FLEET.put(KEY, JSON.stringify(body));
      return new Response(
        JSON.stringify({ ok: true, trucks: (body.trucks || []).length }),
        { status: 200, headers: { ...headers, "Content-Type": "application/json" } }
      );
    }

    if (request.method === "GET") {
      if (!sameToken(bearer(request), env.READ_TOKEN)) {
        return new Response("forbidden", { status: 403, headers });
      }
      const stored = await env.FLEET.get(KEY);
      if (!stored) {
        return new Response(JSON.stringify({ trucks: [], relayed_at: null }), {
          status: 200,
          headers: { ...headers, "Content-Type": "application/json" },
        });
      }
      return new Response(stored, {
        status: 200,
        headers: {
          ...headers,
          "Content-Type": "application/json",
          // NOT publicly cacheable. This was "public, max-age=60" and a
          // browser served the cached authorized response to a request
          // carrying a WRONG token - the check never ran. A shared cache
          // could have handed the positions to anyone. Vary is belt and
          // braces on top of no-store.
          "Cache-Control": "no-store, private",
          "Vary": "Authorization",
        },
      });
    }

    return new Response("method not allowed", { status: 405, headers });
  },
};
