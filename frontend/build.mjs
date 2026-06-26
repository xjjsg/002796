import { build } from "esbuild";
import { mkdir, rm, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(root, "../sz002796/web_assets");

await rm(outDir, { recursive: true, force: true });
await mkdir(path.join(outDir, "assets"), { recursive: true });

await build({
  entryPoints: [path.join(root, "src/main.jsx")],
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ["chrome110", "edge110"],
  outfile: path.join(outDir, "assets/app.js"),
  define: {
    "process.env.NODE_ENV": '"production"',
  },
  loader: {
    ".js": "jsx",
    ".jsx": "jsx",
  },
});

await writeFile(
  path.join(outDir, "index.html"),
  `<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#0b0f14" />
    <title>002796.SZ V6 策略工作台</title>
    <link rel="stylesheet" href="/assets/app.css" />
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/assets/app.js"></script>
  </body>
</html>
`,
  "utf8",
);

console.log(`Built dashboard assets in ${outDir}`);
