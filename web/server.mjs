// Production web server: Next behind a plain Node HTTP server that stamps the real client address.
//
//   NODE_ENV=production node server.mjs        (npm start; PORT defaults to 3000)
//
// Deployment requirement: browsers must connect to THIS process directly (or through a TLS layer
// that hands over the original TCP peer, such as a TCP pass-through). If you put an HTTP reverse
// proxy in front of it, every request comes from that proxy and all clients share one rate-limit
// bucket again; extend stampClientIp to read that proxy's header from that proxy's address only.
// The web server and the API share THALLO_PROXY_STAMP_KEY. The API should still accept connections
// only from this server (firewall, and THALLO_WEB_PROXY_HOSTS), never directly from the internet.

import { createServer } from "node:http";

import next from "next";

import { stampClientIp, stampKeyFromEnv } from "./lib/clientip.mjs";

const port = Number.parseInt(process.env.PORT ?? "3000", 10);
const hostname = process.env.HOSTNAME ?? "127.0.0.1";
const app = next({ dev: false, dir: new URL(".", import.meta.url).pathname });
const handle = app.getRequestHandler();
const stampKey = stampKeyFromEnv(process.env);
if (!stampKey) console.warn("THALLO_PROXY_STAMP_KEY is not set: client addresses are not stamped (development only)");

await app.prepare();
createServer((req, res) => {
  stampClientIp(req, req.socket.remoteAddress, stampKey);
  handle(req, res);
}).listen(port, hostname, () => {
  console.log(`thallo web on http://${hostname}:${port}`);
});
