import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs';
import { resolve, join, relative } from 'node:path';
const root = resolve('dist');
const base = (process.env.DOCS_BASE || '/').replace(/\/$/, '');
const walk = (dir) => readdirSync(dir, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)]);
const html = walk(root).filter(p => p.endsWith('.html'));
const ids = new Map(html.map(p => [p, new Set([...readFileSync(p, 'utf8').matchAll(/\bid="([^"]+)"/g)].map(m => m[1]))]));
let errors = [];
for (const file of html) {
  const text = readFileSync(file, 'utf8');
  const route = '/' + relative(root, file).replace(/index\.html$/, '');
  for (const [, raw] of text.matchAll(/\b(?:href|src)="([^"]+)"/g)) {
    if (/^(https?:|mailto:|data:|javascript:|tel:)/.test(raw)) continue;
    const url = new URL(raw.replaceAll('&amp;', '&'), 'https://local.invalid' + base + route);
    let path = decodeURIComponent(url.pathname);
    if (base && !(path === base || path.startsWith(base + '/'))) {
      errors.push(`${relative(root, file)}: outside base: ${raw}`); continue;
    }
    path = path.slice(base.length);
    let target = resolve(root, '.' + path);
    if (existsSync(target) && statSync(target).isDirectory()) target = join(target, 'index.html');
    if (!existsSync(target)) errors.push(`${relative(root, file)}: missing ${raw}`);
    else if (url.hash && ids.has(target) && !ids.get(target).has(decodeURIComponent(url.hash.slice(1)))) errors.push(`${relative(root, file)}: missing fragment ${raw}`);
  }
}
if (errors.length) { console.error(errors.join('\n')); process.exit(1); }
console.log(`Validated internal URLs, assets and fragments in ${html.length} HTML pages (base ${base || '/'}).`);
