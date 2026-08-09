// Serialize with the vendored bafang_canable_pro serializer.
//
// Fakes the canbus instance so the frames the serializer would transmit are
// captured as "<hex id>#<hex data>" strings instead. Used by
// test_differential.py to check both the multi-frame framing and the byte
// offsets the Python encoders use.

"use strict";

const path = require("node:path");

// The vendored serializer logs its progress to stdout, which is our result
// channel; send its chatter to stderr instead.
console.log = (...args) => process.stderr.write(args.join(" ") + "\n");
console.warn = console.log;

const vendor = path.resolve(__dirname, "../../vendor/bafang_canable_pro");
const serializer = require(path.join(vendor, "bafang-serializer.js"));

function fakeCanbus() {
  const frames = [];
  return {
    frames,
    isConnected: () => true,
    emit: (event, message) => {
      throw new Error(`serializer error: ${event} ${message}`);
    },
    sendFrame: async (command) => {
      frames.push(command);
      return true;
    },
  };
}

async function run(request) {
  const bus = fakeCanbus();
  switch (request.kind) {
    case "long_write":
      await serializer.writeLongParameter(
        bus,
        request.target,
        { canCommandCode: request.code, canCommandSubCode: request.subcode },
        request.data,
      );
      break;
    case "parameter0":
      serializer.prepareParameter0WriteData(bus, request.value);
      break;
    case "parameter1":
      serializer.prepareParameter1WriteData(bus, request.value);
      break;
    case "parameter2":
      serializer.prepareParameter2WriteData(bus, request.value);
      break;
    default:
      throw new Error(`unknown request kind: ${request.kind}`);
  }
  // The parameterN helpers kick off an async multi-frame send; give it time.
  await new Promise((resolve) => setTimeout(resolve, 400));
  return bus.frames;
}

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", async () => {
  const { requests } = JSON.parse(input);
  const results = [];
  for (const request of requests) {
    results.push(await run(request));
  }
  process.stdout.write(JSON.stringify(results));
});
