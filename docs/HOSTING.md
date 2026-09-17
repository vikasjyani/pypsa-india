# Hosting the PyPSA-India user guide

Use the **pypsa-india project root**: the directory containing `Snakefile`,
`docs/`, `tools/` and `.github/`. The public site needs no model server,
database or solver. Its search and command helper run in the browser.

## Build and preview

Build the self-contained static site from the project root:

```powershell
python tools/build_docs.py
```

The standard-library-only builder creates `_site/` from an explicit allowlist:
the user guide as `index.html` and `guide.html`, the three portable files in
`docs/examples/beginner_demo/`, and `docs/downloads/beginner_demo.zip`. It
also writes `.nojekyll`, fails on missing sources, rejects symlink output, and
only cleans the resolved project `_site/` directory.

Preview it locally with any static server:

```powershell
python -m http.server 8000 --bind 127.0.0.1 --directory _site
```

Open [the local preview](http://127.0.0.1:8000/) and check chapter navigation,
search, a copied command, and the beginner ZIP download. Stop the preview
server with Ctrl+C in that terminal. `_site/` is generated output; each build
replaces it. Edit the source files under `docs/` instead.

## Publish with GitHub Pages

1. Place this project's files in your chosen GitHub repository. The repository
   root must contain `tools/build_docs.py`, `docs/guide.html`, the example and
   download files, and `.github/workflows/docs-pages.yml`. If the project is
   nested under another directory, adjust the workflow's build directory and
   artifact path first; GitHub discovers workflows only in the repository's
   root `.github/workflows/` directory.
2. In that repository, open **Settings → Pages → Build and deployment** and
   choose **GitHub Actions** as the source.
3. Open **Actions → Publish documentation → Run workflow**, choose the branch
   containing the guide, and run it. The workflow builds `_site/`, uploads the
   static artifact and deploys it to the `github-pages` environment.
4. When deployment succeeds, use the page URL shown by the deployment or
   **Settings → Pages**. Test the download at that URL too.

The workflow runs manually; a normal push does not publish the site. Relative
links work at a domain root or below a repository project path. The artifact
contains only the allowlisted guide and teaching inputs. Internal audit
records, study results and other workbooks are excluded from the site build.

## Update the guide

- Edit `docs/guide.html`; it is the single source for the published home page
  and the `guide.html` alias. CSS, JavaScript and diagrams stay inline.
- Edit the three files in `docs/examples/beginner_demo/` when changing the
  teaching example. Keep its workbook and YAML consistent with the tutorial.
- After an example change, rebuild the ZIP from the **docs** directory so it
  contains the `beginner_demo/` folder:

  ```powershell
  Push-Location docs
  Compress-Archive -Path examples/beginner_demo -DestinationPath downloads/beginner_demo.zip -Force
  Pop-Location
  ```

- Rebuild, preview, commit the changed sources, and run the Pages workflow.
  Do not maintain a separate manual copy of `_site/index.html`.

To use another static host, upload the contents of `_site/` with its directory
structure intact and use `index.html` as the entry page.

See GitHub's [custom workflows guide](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
for Pages setup and permissions.
