// The one place the real client address enters the system. The web server (server.mjs) calls this
// on every request before Next sees it: any client-sent copy of the stamp headers (and of the
// forwarding headers) is removed, then the socket peer is written together with an HMAC of
// (address, unix seconds) under THALLO_PROXY_STAMP_KEY. Next's /api rewrite forwards both to the
// API, which honours the address only when the HMAC verifies and the stamp is under 30 seconds
// old (api/sessions.py, verified_stamp). Without a key nothing is stamped.

import { createHmac } from "node:crypto";

export const CLIENT_IP_HEADER = "x-thallo-client-ip";
export const CLIENT_STAMP_HEADER = "x-thallo-client-stamp";
const DROP = new Set([CLIENT_IP_HEADER, CLIENT_STAMP_HEADER, "x-forwarded-for", "x-real-ip", "forwarded"]);

export function normalisePeer(address) {
  if (!address) return "unknown";
  return address.startsWith("::ffff:") ? address.slice(7) : address; // IPv4 mapped into IPv6
}

export function stampMac(key, ip, ts) {
  return createHmac("sha256", key).update(`${ip}|${ts}`).digest("hex");
}

/** Mutates a Node IncomingMessage-like object: `headers` (lowercase keys) and `rawHeaders`.
 *  `key` is a Buffer (or null: strip only); `now` is unix seconds. */
export function stampClientIp(req, remoteAddress, key, now = Math.floor(Date.now() / 1000)) {
  for (const name of Object.keys(req.headers)) {
    if (DROP.has(name.toLowerCase())) delete req.headers[name];
  }
  if (Array.isArray(req.rawHeaders)) {
    const kept = [];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      if (!DROP.has(String(req.rawHeaders[i]).toLowerCase())) kept.push(req.rawHeaders[i], req.rawHeaders[i + 1]);
    }
    req.rawHeaders = kept;
  }
  const peer = normalisePeer(remoteAddress);
  if (!key) return peer;
  const stamp = `${now}.${stampMac(key, peer, now)}`;
  req.headers[CLIENT_IP_HEADER] = peer;
  req.headers[CLIENT_STAMP_HEADER] = stamp;
  if (Array.isArray(req.rawHeaders)) req.rawHeaders.push(CLIENT_IP_HEADER, peer, CLIENT_STAMP_HEADER, stamp);
  return peer;
}

export function stampKeyFromEnv(env) {
  const hex = env.THALLO_PROXY_STAMP_KEY ?? "";
  return /^(?:[0-9a-fA-F]{2}){32,}$/.test(hex) ? Buffer.from(hex, "hex") : null;
}
