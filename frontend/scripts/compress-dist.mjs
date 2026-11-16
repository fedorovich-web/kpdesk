import { readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { extname, join } from 'node:path';
import { brotliCompressSync, constants, gzipSync } from 'node:zlib';

const directory = new URL('../dist/', import.meta.url);
const supported = new Set(['.html', '.js', '.mjs', '.css', '.svg', '.json', '.xml', '.txt']);

async function compressTree(path) {
  let count = 0;
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const filename = join(path, entry.name);
    if (entry.isDirectory()) {
      count += await compressTree(filename);
    } else if (entry.isFile() && supported.has(extname(entry.name))) {
      if ((await stat(filename)).size < 1024) continue;
      const bytes = await readFile(filename);
      const brotli = brotliCompressSync(bytes, {
        params: { [constants.BROTLI_PARAM_QUALITY]: 7 },
      });
      const gzip = gzipSync(bytes, { level: 6 });
      await Promise.all([
        writeFile(filename + '.br', brotli),
        writeFile(filename + '.gz', gzip),
      ]);
      count += 1;
    }
  }
  return count;
}

const total = await compressTree(directory.pathname);
console.log('Precompressed ' + total + ' text resources for Brotli/Gzip static delivery.');
