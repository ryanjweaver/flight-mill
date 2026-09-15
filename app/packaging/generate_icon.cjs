/**
 * Generate the Windows icon from the app's original bee.svg.
 * Requires Node.js and sharp. If sharp is bundled elsewhere, set
 * FLIGHTMILL_NODE_MODULES to its node_modules directory before running:
 *   node app/packaging/generate_icon.cjs
 * The ICO contains lossless PNG frames for Windows Vista and later.
 */
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const sharp = require(process.env.FLIGHTMILL_NODE_MODULES
  ? path.join(process.env.FLIGHTMILL_NODE_MODULES, "sharp")
  : "sharp");

async function main() {
  const source = fs.readFileSync(path.resolve(__dirname, "../src/flightmill/ui/static/assets/bee.svg"));
  const sizes = [16, 24, 32, 48, 64, 128, 256];
  const frames = await Promise.all(sizes.map(size =>
    sharp(source, { density: 384 }).resize(size, size).png().toBuffer()));
  const directory = Buffer.alloc(6 + sizes.length * 16);
  directory.writeUInt16LE(1, 2); // Windows icon
  directory.writeUInt16LE(sizes.length, 4);
  let offset = directory.length;
  frames.forEach((frame, index) => {
    const entry = 6 + index * 16;
    directory.writeUInt8(sizes[index] === 256 ? 0 : sizes[index], entry);
    directory.writeUInt8(sizes[index] === 256 ? 0 : sizes[index], entry + 1);
    directory.writeUInt16LE(1, entry + 4); // planes
    directory.writeUInt16LE(32, entry + 6); // RGBA
    directory.writeUInt32LE(frame.length, entry + 8);
    directory.writeUInt32LE(offset, entry + 12);
    offset += frame.length;
  });
  const destination = path.join(__dirname, "flightmill.ico");
  fs.writeFileSync(destination, Buffer.concat([directory, ...frames]));
  process.stdout.write("Created " + destination + " (" + sizes.join(", ") + " px)\n");
}
main().catch(error => { process.stderr.write(error.message + "\n"); process.exitCode = 1; });
