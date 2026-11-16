import { readFile, stat } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..', 'dist', 'assets');
const sources = {
  hero: ['01_hero_main', '12_hero_offer_stack'],
  icons: [
    '01_document_add', '06_settings', '12_team', '22_success_check',
    '27_database', '31_lightning', '33_analytics',
  ],
};

for (const [folder, names] of Object.entries(sources)) {
  for (const name of names) {
    const full = resolve(root, folder, name + '.avif');
    const original = await stat(full);
    const widths = folder === 'hero' ? [480, 800] : [96, 144];
    let previous = 0;
    for (const width of widths) {
      const file = resolve(root, folder, name + '-' + width + 'w.avif');
      const bytes = await readFile(file);
      if (bytes.length < 20 || bytes.subarray(4, 12).toString('ascii') !== 'ftypavif') {
        throw new Error('Invalid AVIF image: ' + file);
      }
      if (bytes.length >= original.size) {
        throw new Error('Variant is not smaller than original: ' + file);
      }
      if (previous && bytes.length < previous) {
        throw new Error('Larger variant is unexpectedly smaller: ' + file);
      }
      previous = bytes.length;
    }
  }
}
console.log('Responsive AVIF assets: verified dimensions tiers, formats and smaller byte sizes.');
