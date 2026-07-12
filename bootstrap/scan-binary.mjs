// scan-binary.mjs - extract the call-home surface (hostnames, env knobs,
// telemetry/analytics keywords) from a compiled binary or any large file by
// scanning embedded printable-string runs. Used to derive and re-verify the
// Kilo hardening profile (engines/kilo/HARDENING.md) after a version bump, and
// by the security sweep to flag unexpected endpoints in vendored artifacts.
//
//   node bootstrap/scan-binary.mjs <path> [--hosts-only]
import fs from "fs";

const target = process.argv[2];
const hostsOnly = process.argv.includes("--hosts-only");
if (!target || !fs.existsSync(target)) {
  console.error("usage: node bootstrap/scan-binary.mjs <path> [--hosts-only]");
  process.exit(2);
}

const HOST = /\b(?:https?:\/\/)?(?:[a-z0-9-]+\.)+(?:ai|com|io|dev|org|net|sh|app|cloud|co)\b(?:\/[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]*)?/g;
const ENVV = /\b(?:KILO|OPENCODE|KILOCODE)_[A-Z0-9_]+\b/g;
const KEYS = /\b(?:sentry|posthog|opentelemetry|otel|telemetry|gateway|marketplace|feedback|autoupdate|amplitude|segment|datadog|analytics|ingest)\w*/gi;

const hosts = new Map(), envs = new Map(), keys = new Map();
const bump = (m, k) => { k = k.toLowerCase(); m.set(k, (m.get(k) || 0) + 1); };

const CH = 8 * 1024 * 1024;
const fd = fs.openSync(target, "r");
const buf = Buffer.alloc(CH);
let carry = "", total = 0;
for (;;) {
  const n = fs.readSync(fd, buf, 0, CH, null);
  if (n <= 0) break;
  total += n;
  const s = carry + buf.toString("latin1", 0, n).replace(/[^\x20-\x7e]+/g, " ");
  for (const m of s.matchAll(HOST)) bump(hosts, m[0]);
  if (!hostsOnly) {
    for (const m of s.matchAll(ENVV)) bump(envs, m[0]);
    for (const m of s.matchAll(KEYS)) bump(keys, m[0]);
  }
  carry = s.slice(-256);
}
fs.closeSync(fd);

const top = (m, n) => [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, v]) => `${v}\t${k}`);
// drop schema/docs/vendor noise so real endpoints stand out
const noise = /(schema\.org|w3\.org|example\.com|localhost|127\.0\.0\.1|xmlns|openxmlformats|purl\.org|\.js$|\.ts$|\.md$|\.json$|\.css$|\.svg$|\.wasm$|\.node$|\.dtd$)/i;
console.log(`# scanned ${(total / 1048576).toFixed(1)} MB of ${target}`);
console.log("\n## HOSTNAMES");
console.log(top(hosts, 500).filter((l) => !noise.test(l)).join("\n"));
if (!hostsOnly) {
  console.log("\n## ENV VARS");
  console.log(top(envs, 300).join("\n"));
  console.log("\n## KEYWORDS");
  console.log(top(keys, 80).join("\n"));
}
