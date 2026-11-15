// The one place the real client address enters the system. The web server (server.mjs) calls this
// on every request before Next sees it: any client-sent copy of the header (or of the forwarding
// headers) is removed, and the header is OVERWRITTEN with the socket peer. Next's /api rewrite then
// forwards it to the API, which trusts it only from this server's address (THALLO_WEB_PROXY_HOSTS).

export const CLIENT_IP_HEADER = "x-thallo-client-ip";
const DROP = new Set([CLIENT_IP_HEADER, "x-forwarded-for", "x-real-ip", "forwarded"]);

export function normalisePeer(address) {
  if (!address) return "unknown";
  return address.startsWith("::ffff:") ? address.slice(7) : address; // IPv4 mapped into IPv6
}

/** Mutates a Node IncomingMessage-like object: `headers` (lowercase keys) and `rawHeaders`. */
export function stampClientIp(req, remoteAddress) {
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
  req.headers[CLIENT_IP_HEADER] = peer;
  if (Array.isArray(req.rawHeaders)) req.rawHeaders.push(CLIENT_IP_HEADER, peer);
  return peer;
}
