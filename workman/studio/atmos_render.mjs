// Render one Atmosmotion composition to a file for workman.studio.assemble.
//
//   nice -n 19 node atmos_render.mjs <comp.cm.json> <out> plate|opaque
//
// plate:  white-on-black text plate, lossless gray FFV1 (.mkv); assemble alphamerges it over the clips.
// opaque: a finished segment (the dark end card), H.264 crf 17 yuv420p (.mp4).
//
// Same pattern as ~/advisorppc-ads-20260925/ident/render-ident.mjs and the engine's
// proofs/self-branding/render-studies.mjs: Composition.parse, nativeBackend.supports must say
// "yes", then frames come from composeAnimatedFrame (the function nativeBackend.render calls per
// frame) piped to our own ffmpeg, because nativeBackend.render hard-codes libx264 crf 23 and has
// no gray or lossless output. The engine path comes from ATMOS_ENGINE (its packages/ folder).
import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { once } from "node:events";

const [compPath, outPath, mode] = process.argv.slice(2);
const ENGINE = process.env.ATMOS_ENGINE;
if (!compPath || !outPath || !["plate", "opaque"].includes(mode) || !ENGINE) {
  console.error("usage: ATMOS_ENGINE=<packages dir> node atmos_render.mjs <comp.cm.json> <out> plate|opaque");
  process.exit(2);
}
const TIMEOUT_MS = Number(process.env.ATMOS_FFMPEG_TIMEOUT_MS || 600000);
const { Composition } = await import(`${ENGINE}/schema/dist/index.js`);
const { nativeBackend, composeAnimatedFrame } = await import(`${ENGINE}/engine-native/dist/index.js`);

const comp = Composition.parse(JSON.parse(readFileSync(compPath, "utf8")));
const support = nativeBackend.supports(comp);
if (support.kind !== "yes") {
  console.error(`nativeBackend.supports=${support.kind} ${JSON.stringify(support.warnings ?? support.reasons ?? [])}`);
  process.exit(3);
}
const { w: W, h: H } = comp.size;
const N = Math.round(comp.duration * comp.fps);
const enc = mode === "plate"
  ? ["-vf", "format=gray", "-c:v", "ffv1", "-pix_fmt", "gray", outPath]
  : ["-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p", "-movflags", "+faststart", outPath];
const ff = spawn("nice", ["-n", "19", "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-threads", "4",
  "-f", "rawvideo", "-pix_fmt", "rgba", "-s", `${W}x${H}`, "-r", `${comp.fps}`, "-i", "pipe:0", ...enc],
  { stdio: ["pipe", "ignore", "pipe"] });
let err = "";
ff.stderr.on("data", (d) => { if (err.length < 8192) err += d.toString(); });
const timer = setTimeout(() => ff.kill("SIGKILL"), TIMEOUT_MS);   // our own child, by its PID
const cache = new Map();
for (let f = 0; f < N; f++) {
  const b = await composeAnimatedFrame(comp, f, cache);
  if (!ff.stdin.write(new Uint8Array(b.data.buffer, b.data.byteOffset, b.data.byteLength))) await once(ff.stdin, "drain");
}
ff.stdin.end();
const [code] = await once(ff, "close");
clearTimeout(timer);
if (code !== 0) {
  console.error(`ffmpeg exited ${code}: ${err}`);
  process.exit(4);
}
console.log(JSON.stringify({ ok: true, out: outPath, frames: N, size: [W, H] }));
