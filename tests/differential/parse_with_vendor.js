// Parse blocks with the vendored bafang_canable_pro parsers.
//
// Reads {"cases":[{"kind":"parameter1","data":[...]}]} on stdin, writes the
// parsed objects as JSON on stdout. Used by test_differential.py to check the
// Python codecs against the upstream implementation they were derived from.

"use strict";

const path = require("node:path");

// Keep vendor logging off our result channel.
console.log = (...args) => process.stderr.write(args.join(" ") + "\n");
console.warn = console.log;

const vendor = path.resolve(__dirname, "../../vendor/bafang_canable_pro");
const {
  BafangCanControllerParser,
  BafangCanBatteryParser,
  BafangCanSensorParser,
  BafangCanDisplayParser,
} = require(path.join(vendor, "bafang-parser.js"));

const parsers = {
  parameter0: (p) => BafangCanControllerParser.parameter0(p),
  parameter1: (p) => BafangCanControllerParser.parameter1(p),
  parameter2: (p) => BafangCanControllerParser.parameter2(p),
  speed: (p) => BafangCanControllerParser.parameter3(p),
  realtime0: (p) => BafangCanControllerParser.package0(p),
  realtime1: (p) => BafangCanControllerParser.package1(p),
  sensor: (p) => BafangCanSensorParser.package0(p),
  battery_state: (p) => BafangCanBatteryParser.state(p),
  battery_capacity: (p) => BafangCanBatteryParser.capacity(p),
  display1: (p) => BafangCanDisplayParser.package1(p),
  display2: (p) => BafangCanDisplayParser.package2(p),
  error_codes: (p) => BafangCanDisplayParser.errorCodes(p.data),
};

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const { cases } = JSON.parse(input);
  const results = cases.map(({ kind, data }) => {
    const parse = parsers[kind];
    if (!parse) throw new Error(`unknown case kind: ${kind}`);
    return parse({ data, canCommandSubCode: 0 });
  });
  process.stdout.write(JSON.stringify(results));
});
