import { describe, expect, it } from "vitest";

import { CLIENT_IP_HEADER, CLIENT_STAMP_HEADER, stampClientIp, stampMac } from "./clientip.mjs";

const KEY = Buffer.alloc(32, 7);

describe("client address stamp", () => {
  it("overwrites spoofed headers and drops forwarding headers", () => {
    const req = {
      headers: { [CLIENT_IP_HEADER]: "6.6.6.6", [CLIENT_STAMP_HEADER]: "1.ff", "x-forwarded-for": "6.6.6.6",
                 "x-real-ip": "6.6.6.6", host: "app" },
      rawHeaders: ["X-Thallo-Client-IP", "6.6.6.6", "X-Forwarded-For", "6.6.6.6", "Host", "app"],
    };
    stampClientIp(req, "203.0.113.5", KEY, 1000);
    expect(req.headers[CLIENT_IP_HEADER]).toBe("203.0.113.5");
    expect(req.headers[CLIENT_STAMP_HEADER]).toBe(`1000.${stampMac(KEY, "203.0.113.5", 1000)}`);
    expect(req.headers["x-forwarded-for"]).toBeUndefined();
    expect(req.headers["x-real-ip"]).toBeUndefined();
    expect(req.rawHeaders.slice(0, 2)).toEqual(["Host", "app"]);
  });

  it("without a key strips and stamps nothing", () => {
    const req = { headers: { [CLIENT_IP_HEADER]: "6.6.6.6" }, rawHeaders: [] };
    stampClientIp(req, "203.0.113.5", null);
    expect(req.headers[CLIENT_IP_HEADER]).toBeUndefined();
  });

  it("matches the API's HMAC byte for byte", () => {
    // Same vector as tests/wp09_api/test_auth_hardening.py::test_stamp_vector_matches_the_web_server
    expect(stampMac(KEY, "198.51.100.1", 1700000000)).toBe(
      "637855c9e0a3ea9128d80d36c9dcaf5b0df2d2eadf1ae7484fcc3c06960fdd86",
    );
  });

  it("gives two real peers two different stamps and unwraps mapped IPv4", () => {
    const a = { headers: {}, rawHeaders: [] };
    const b = { headers: {}, rawHeaders: [] };
    expect(stampClientIp(a, "::ffff:198.51.100.1", KEY, 1)).toBe("198.51.100.1");
    expect(stampClientIp(b, "198.51.100.2", KEY, 1)).toBe("198.51.100.2");
    expect(a.headers[CLIENT_STAMP_HEADER]).not.toBe(b.headers[CLIENT_STAMP_HEADER]);
  });
});
