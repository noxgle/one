# ONE documentation portal

Astro + Starlight, English Markdown, static HTML and Pagefind search. No server
runtime, credentials, automatic deployment, or change to `landing_page/`.

## Local workflow

Use **Node.js 22.23.4+** (Astro 7 requires `>=22.23.4`) and npm 10.8.2+.
From this directory:

```sh
npm ci
npm run dev
```

Open the localhost URL printed by Astro. For a production build:

```sh
npm run build
npm run check:links
npm run preview
```

Search is indexed in the production build; verify it in preview, not only dev.
The output is `website/dist/`. Open it through an HTTP server, not `file://`.
Do not upload sources, `node_modules`, or ONE state directories.

## Domain and subdirectory

No production hostname is assumed. At build time set the actual public origin
and optional base path (repeat them for preview and link checks):

```sh
DOCS_SITE=https://docs.example.org DOCS_BASE=/one/ npm run build
DOCS_BASE=/one/ npm run check:links
DOCS_BASE=/one/ npm run preview
```

`DOCS_SITE` must be an absolute URL. `DOCS_BASE` defaults to `/`; use leading and
trailing slashes. The site is not a single-page app: serve directory `index.html`
and preserve `_astro/` and `pagefind/`. Do not rewrite all requests to the root.

## Manual server upload

1. Build locally and run the checks above with the intended hostname/base.
2. In preview, test navigation, dark mode, mobile layout and search for `readonly`,
   `ONE_CODING_AGENT_DIR`, `one run`, `MCP` and `compaction`.
3. Back up the currently published directory. Upload **the contents** of `dist/`
   into a new staging/release directory using your existing SSH/SFTP credentials.
   Example only; replace paths and host after verifying the target:

   ```sh
   rsync -av --dry-run dist/ USER@HOST:/srv/www/one-docs-next/
   rsync -av dist/ USER@HOST:/srv/www/one-docs-next/
   ```

4. On the server, point the web root (or `/one/` alias) to that directory using
   your existing server configuration. Prefer an atomic release/symlink switch.
   No remote commands, credentials or destructive `--delete` flags are built in.
5. Test the public homepage, a nested page, a search query, assets and a deliberate
   missing URL. Keep the old release until checks pass; rollback means switching
   the root back to it. Configure HTTPS and caching on your server; avoid caching
   HTML indefinitely across releases.

CI builds and checks only. It neither publishes nor connects to a server.

## Maintain content

Canonical articles live in `src/content/docs/`. Old `docs/*.md` files are short
compatibility pointers, not parallel copies. Update the portal article itself.
The original README was split into `handbook/`; see `maintainers/migration.md`.

Regenerate code inventories from the repository root with an isolated state:

```sh
export ONE_CODING_AGENT_DIR="$(mktemp -d)"
.venv/bin/python scripts/generate_docs_reference.py
```

Review changes rather than treating generated defaults as complete semantics.
Keep relative Markdown article links in new content; the build rewrites them to
base-aware portal routes. `npm run check:links` validates all built internal
links, fragments, scripts and images, but deliberately never contacts external
sites. Existing GitHub fragment bookmarks cannot be redirected by static Markdown;
old file paths remain navigable via compatibility pointers.

## Integrated landing/documentation deploy script

From the repository root (Python 3, curl and Node.js 22.23.4+ on the local machine):

```sh
./landing_page/deploy.sh --docs --dry-run
./landing_page/deploy.sh --docs
./landing_page/deploy.sh --all
```

Without arguments, only the landing is uploaded, as before. `--landing` selects
that explicitly. `--docs` runs `npm ci`, builds locally and checks links before
recursively uploading **only static `website/dist/` contents**. `--all` prepares
both targets before any upload. The server needs no Node.js or Python.

Use the existing ignored `landing_page/.env`; see
[`deploy.env.example`](../landing_page/deploy.env.example) for supported keys.
The env file overrides environment variables; `--env-file PATH` selects another
file. Values are literal, optionally quoted, without shell expansion, `export`
prefixes or inline comments. Unknown keys are rejected. Credentials are not
needed for `--dry-run`; FTP_HOST and FTP_DIR still define the proposed destination.
Dry runs install/build locally and may contact the npm registry, but never the
deployment server.

Set `PUBLIC_SITE_URL` to the actual HTTPS origin (not the FTP hostname).
`DOCS_BASE=/docs/` maps to `FTP_DIR/docs/` and configures local asset/search URLs.
For example, FTP_DIR=/public_html gives /public_html/docs/index.html.
A root documentation base is refused to protect the landing.

**Transport change:** explicit FTPS is required by default (`FTP_TLS=true`), with
normal certificate validation and no fallback. If the existing hosting only
supports plain FTP, `FTP_TLS=false` explicitly opts into sending credentials and
content unencrypted; the script warns. Confirm hosting support before deployment.
SFTP is not implemented. Credentials are passed to curl through stdin, not argv.

Files are uploaded without remote deletion, assets before HTML and entry page
last. This is **not atomic**: a failure can leave a partial site, and stale files
remain. Back up the previous release separately and keep a copy of its static
artifact for rollback by re-upload. The script does not back up the server,
perform remote switching, or verify public HTTP responses automatically. Check
the public docs homepage, nested page and search manually after deployment.

### Self-signed FTPS certificates

For a server you deliberately accept with a self-signed certificate:

```sh
./landing_page/deploy.sh --docs --allow-self-signed
```

To persist this choice, set `FTP_ALLOW_SELF_SIGNED=true` in the ignored
`landing_page/.env` (default: false). This passes `--insecure` only to the FTPS
upload: it disables **all certificate and hostname verification**, not just
self-signed certificate rejection. TLS remains mandatory; combining this mode
with `FTP_TLS=false` is rejected. The script warns on each invocation because
an attacker impersonating the server could obtain credentials or modify uploads.
It does not change npm or public HTTPS certificate verification. Prefer a valid
server certificate where possible. No upload occurs with `--dry-run`.
