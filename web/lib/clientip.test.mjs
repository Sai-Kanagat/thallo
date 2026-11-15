import { describe, expect, it } from "vitest";

import { CLIENT_IP_HEADER, stampClientIp } from "./clientip.mjs";

describe("client address stamp", () => {
  it("overwrites a spoofed header and drops forwarding headers", () => {
    const req = {
      headers: { [CLIENT_IP_HEADER]: "6.6.6.6", "x-forwarded-for": "6.6.6.6", "x-real-ip": "6.6.6.6", host: "app" },
      rawHeaders: ["X-Thallo-Client-IP", "6.6.6.6", "X-Forwarded-For", "6.6.6.6", "Host", "app"],
    };
    stampClientIp(req, "203.0.113.5");
    expect(req.headers[CLIENT_IP_HEADER]).toBe("203.0.113.5");
    expect(req.headers["x-forwarded-for"]).toBeUndefined();
    expect(req.headers["x-real-ip"]).toBeUndefined();
    expect(req.rawHeaders).toEqual(["Host", "app", CLIENT_IP_HEADER, "203.0.113.5"]);
  });

  it("gives two real peers two different stamps and unwraps mapped IPv4", () => {
    const a = { headers: {}, rawHeaders: [] };
    const b = { headers: {}, rawHeaders: [] };
    expect(stampClientIp(a, "::ffff:198.51.100.1")).toBe("198.51.100.1");
    expect(stampClientIp(b, "198.51.100.2")).toBe("198.51.100.2");
    expect(a.headers[CLIENT_IP_HEADER]).not.toBe(b.headers[CLIENT_IP_HEADER]);
  });
});
