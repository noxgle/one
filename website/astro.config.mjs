import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

// Markdown source links remain useful on GitHub; compile them to portal routes.
function portalLinks() {
  return (tree, file) => {
    const visit = (node) => {
      if (node.type === 'link' && node.url && !/^(https?:|mailto:|#)/.test(node.url)) {
        let url = node.url;
        if (!url.startsWith('/')) {
          const source = file.history[0].split('/src/content/docs/')[1];
          url = new URL(url, 'https://docs.invalid/' + source).pathname + (url.includes('#') ? '#' + url.split('#')[1] : '');
        }
        url = url.replace(/\.md(?=#|$)/, '/');
        node.url = (process.env.DOCS_BASE || '/').replace(/\/$/, '') + url;
      }
      for (const child of node.children || []) visit(child);
    };
    visit(tree);
  };
}
export default defineConfig({
  site: process.env.DOCS_SITE || undefined,
  base: process.env.DOCS_BASE || '/',
  output: 'static',
  markdown: { remarkPlugins: [portalLinks] },
  integrations: [starlight({
    title: 'ONE',
    description: 'Local-first autonomous terminal agent — guides and reference',
    social: [{ icon: 'github', label: 'GitHub', href: 'https://github.com/noxgle/one' }],
    sidebar: [
      { label: 'Start here', items: [{ slug: 'index' }, { slug: 'library/quickstart' }, { slug: 'guides/local-first-quickstart' }, { slug: 'handbook/installation' }] },
      { label: 'Task guides', items: [{ autogenerate: { directory: 'guides' } }] },
      { label: 'User handbook', items: [{ autogenerate: { directory: 'handbook' } }] },
      { label: 'Reference', items: [{ autogenerate: { directory: 'reference' } }] },
      { label: 'In depth', items: [{ autogenerate: { directory: 'library' } }] },
      { label: 'Maintain this portal', items: [{ autogenerate: { directory: 'maintainers' } }] },
    ],
  })],
});
